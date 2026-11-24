"""SQLite registry, migrations and literal evidence retrieval."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import (
    URL,
    Column,
    Connection,
    Table,
    bindparam,
    create_engine,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from ira_native.adapters.schema_v1 import assets, documents, evidence, revisions, runs
from ira_native.domain.models import Document, DocumentRevision, Evidence, ParseRun, SourceAsset
from ira_native.errors import IraError

Backend = Literal["fts5", "like", "exact"]


@dataclass(frozen=True)
class AssetInput:
    asset: SourceAsset
    data: bytes | None = None


class Registry:
    def __init__(self, database: Path):
        try:
            database.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise IraError("storage_error") from None
        self.engine = create_engine(
            URL.create("sqlite", database=str(database.resolve())), hide_parameters=True
        )
        event.listen(self.engine, "connect", self._connection_setup)
        try:
            self._migrate()
        except BaseException:
            self.engine.dispose()
            raise

    @staticmethod
    def _connection_setup(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")

    def _migrate(self) -> None:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parent / "migrations").replace("%", "%%")
        )
        with self._transaction() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            config.attributes["connection"] = conn
            try:
                command.upgrade(config, "head")
            except CommandError:
                raise IraError("storage_error") from None
            self.fts_available = (
                conn.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE name = 'evidence_fts'"
                ).scalar()
                is not None
            )
            if self.fts_available:
                try:
                    conn.exec_driver_sql("SELECT rowid FROM evidence_fts LIMIT 0")
                except SQLAlchemyError as error:
                    if "no such module: fts5" in str(getattr(error, "orig", "")).lower():
                        raise IraError("fts_unavailable") from None
                    raise

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        try:
            with self.engine.begin() as conn:
                yield conn
        except SQLAlchemyError:
            raise IraError("storage_error") from None

    @staticmethod
    def _insert_asset(conn: Connection, source: AssetInput) -> SourceAsset:
        record = source.asset
        values = record.model_dump(mode="json")
        values["locator_internal"] = record.locator_internal
        values["asset_data"] = source.data
        conn.execute(
            insert(assets).values(values).on_conflict_do_nothing(index_elements=[assets.c.asset_id])
        )
        return Registry._read_asset(conn, record.asset_id)

    @staticmethod
    def _read_asset(conn: Connection, asset_id: str) -> SourceAsset:
        row = (
            conn.execute(select(*_asset_columns()).where(assets.c.asset_id == asset_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise IraError("source_missing")
        return SourceAsset.model_validate(dict(row))

    def register_asset(self, source: AssetInput) -> SourceAsset:
        with self._transaction() as conn:
            return self._insert_asset(conn, source)

    def get_asset(self, asset_id: str) -> SourceAsset:
        with self._transaction() as conn:
            return self._read_asset(conn, asset_id)

    def list_assets(self) -> list[SourceAsset]:
        with self._transaction() as conn:
            return [
                SourceAsset.model_validate(dict(row))
                for row in conn.execute(select(*_asset_columns())).mappings()
            ]

    def get_asset_data(self, asset_id: str) -> bytes | None:
        with self._transaction() as conn:
            return conn.execute(
                select(assets.c.asset_data).where(assets.c.asset_id == asset_id)
            ).scalar_one_or_none()

    def register_revision(self, document: Document, revision: DocumentRevision) -> None:
        with self._transaction() as conn:
            self._insert_explicit_record(
                conn,
                documents,
                documents.c.document_id,
                document.model_dump(),
                document.document_id,
            )
            self._insert_explicit_record(
                conn,
                revisions,
                revisions.c.revision_id,
                revision.model_dump(),
                revision.revision_id,
            )

    @staticmethod
    def _insert_explicit_record(
        conn: Connection, table: Table, key: Column, values: dict, identity: str
    ) -> None:
        conn.execute(insert(table).values(values).on_conflict_do_nothing(index_elements=[key]))
        persisted = conn.execute(select(table).where(key == identity)).mappings().one()
        if dict(persisted) != values:
            raise IraError("registry_conflict")

    def list_revisions(self, asset_id: str) -> list[DocumentRevision]:
        with self._transaction() as conn:
            rows = conn.execute(
                select(revisions)
                .where(revisions.c.asset_id == asset_id)
                .order_by(revisions.c.revision_id)
            ).mappings()
            return [DocumentRevision.model_validate(dict(row)) for row in rows]

    def save_run(
        self,
        run: ParseRun,
        records: Sequence[tuple[Evidence, str]],
        new_assets: Sequence[AssetInput],
        verify_assets: Callable[[SourceAsset, SourceAsset], None],
    ) -> tuple[ParseRun, int]:
        with self._transaction() as conn:
            for source in new_assets:
                self._insert_asset(conn, source)
            verify_assets(
                self._read_asset(conn, run.source_asset_id),
                self._read_asset(conn, run.artifact_asset_id),
            )
            inserted = conn.execute(
                insert(runs)
                .values(run.model_dump(mode="json"))
                .on_conflict_do_nothing(index_elements=[runs.c.parse_run_id])
            ).rowcount
            if inserted and records:
                conn.execute(
                    evidence.insert(),
                    [
                        {
                            "evidence_id": item.evidence_id,
                            "parse_run_id": item.parse_run_id,
                            "source_pointer": item.source_pointer,
                            "kind": item.kind,
                            "page_index": item.page_index,
                            "record": item.model_dump(mode="json"),
                            "search_text": search_text,
                        }
                        for item, search_text in records
                    ],
                )
            persisted = self._read_run(conn, run.parse_run_id)
            count = conn.execute(
                select(func.count())
                .select_from(evidence)
                .where(evidence.c.parse_run_id == run.parse_run_id)
            ).scalar_one()
            return persisted, count

    @staticmethod
    def _read_run(conn: Connection, parse_run_id: str) -> ParseRun:
        row = (
            conn.execute(select(runs).where(runs.c.parse_run_id == parse_run_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise IraError("parse_run_not_found")
        return ParseRun.model_validate(dict(row))

    def get_run(self, parse_run_id: str) -> ParseRun:
        with self._transaction() as conn:
            return self._read_run(conn, parse_run_id)

    def get_evidence(self, evidence_id: str) -> Evidence:
        with self._transaction() as conn:
            record = conn.execute(
                select(evidence.c.record).where(evidence.c.evidence_id == evidence_id)
            ).scalar_one_or_none()
        if record is None:
            raise IraError("no_evidence_found")
        return Evidence.model_validate(record)

    def list_run_evidence(self, parse_run_id: str) -> list[Evidence]:
        query = select(evidence.c.record).where(evidence.c.parse_run_id == parse_run_id)
        with self._transaction() as conn:
            return [
                Evidence.model_validate(value)
                for value in conn.execute(
                    query.order_by(evidence.c.page_index, evidence.c.row_id)
                ).scalars()
            ]

    def search(
        self,
        query: str,
        backend: Backend,
        allowed_asset_ids: Sequence[str],
        *,
        source_asset_id: str | None,
        parse_run_id: str | None,
        physical_page: int | None,
        kind: str | None,
        limit: int,
        project: Callable[[Evidence], Evidence | None],
    ) -> list[Evidence]:
        statement = (
            select(evidence.c.record)
            .join(runs, evidence.c.parse_run_id == runs.c.parse_run_id)
            .where(
                runs.c.source_asset_id.in_(allowed_asset_ids),
                runs.c.artifact_asset_id.in_(allowed_asset_ids),
            )
        )
        if source_asset_id is not None:
            statement = statement.where(runs.c.source_asset_id == source_asset_id)
        if parse_run_id is not None:
            statement = statement.where(runs.c.parse_run_id == parse_run_id)
        if physical_page is not None:
            statement = statement.where(evidence.c.page_index == physical_page - 1)
        if kind is not None:
            statement = statement.where(evidence.c.kind == kind)
        # The stored index joins searchable segments. Validate their boundaries before
        # limiting results so cross-segment matches never suppress valid later hits.
        statement = statement.where(evidence.c.search_text.contains(query, autoescape=True))
        if backend == "fts5":
            phrase = '"' + query.replace('"', '""') + '"'
            statement = statement.where(
                evidence.c.row_id.in_(
                    select(text("rowid"))
                    .select_from(text("evidence_fts"))
                    .where(
                        text("evidence_fts MATCH :phrase").bindparams(bindparam("phrase", phrase))
                    )
                )
            )
        statement = statement.order_by(evidence.c.page_index, evidence.c.row_id)
        with self._transaction() as conn:
            matches = (
                match
                for row in conn.execute(statement).scalars()
                if (match := project(Evidence.model_validate(row))) is not None
            )
            return list(islice(matches, limit))

    def close(self) -> None:
        self.engine.dispose()


def _asset_columns() -> list[Column]:
    return [column for column in assets.columns if column is not assets.c.asset_data]
