"""SQLite persistence for verification records."""

import json

from sqlalchemy import func, literal_column, select, true
from sqlalchemy.dialects.sqlite import insert

from ira_native.adapters.schema_v1 import runs as parse_runs
from ira_native.adapters.workflow_schema import (
    agent_runs,
    checks,
    drafts,
    human_reviews,
    reviews,
    sheet_imports,
    sheets,
    specification_names,
)
from ira_native.errors import IraError

_RECORDS = {"draft": drafts, "sheet": sheet_imports, "run": checks}
_EVENTS = {"review": reviews, "agent": agent_runs}


class WorkflowStore:
    def __init__(self, registry):
        self.registry = registry

    def register_sheet(self, metadata: dict, imported: dict, data: bytes) -> None:
        with self.registry._transaction() as conn:
            asset_id = metadata["asset_id"]
            conn.execute(
                insert(sheets)
                .values(asset_id=asset_id, record=metadata, data=data)
                .on_conflict_do_nothing()
            )
            conn.execute(
                insert(sheet_imports)
                .values(asset_id=asset_id, import_id=imported["import_id"], record=imported)
                .on_conflict_do_nothing()
            )

    def put(self, kind: str, record: dict, **columns) -> None:
        table = _RECORDS[kind]
        with self.registry._transaction() as conn:
            conn.execute(insert(table).values(record=record, **columns).on_conflict_do_nothing())

    def get(self, kind: str, identity: str) -> dict:
        table = _RECORDS[kind]
        key = next(iter(table.primary_key.columns))
        with self.registry._transaction() as conn:
            value = conn.execute(select(table.c.record).where(key == identity)).scalar_one_or_none()
        if value is None:
            raise IraError("not_found")
        return value

    @staticmethod
    def _latest_selection(draft_id: str):
        return (
            select(checks)
            .where(
                checks.c.draft_id == draft_id,
                func.json_type(checks.c.record, "$.host_compilation").is_(None),
            )
            .order_by(literal_column("rowid").desc())
            .limit(1)
        )

    def latest_selection(self, draft_id: str) -> dict | None:
        with self.registry._transaction() as conn:
            row = conn.execute(self._latest_selection(draft_id)).mappings().first()
            return row["record"] if row else None

    def put_run(self, record: dict, expected_run: str | None) -> None:
        with self.registry._transaction() as conn:
            if expected_run is not None:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                latest = conn.execute(
                    self._latest_selection(record["draft_id"]).with_only_columns(checks.c.run_id)
                ).scalar_one_or_none()
                if (latest or "") != expected_run:
                    raise IraError("selection_changed")
            conn.execute(
                insert(checks).values(
                    run_id=record["run_id"], draft_id=record["draft_id"], record=record
                )
            )

    def append(self, kind: str, run_id: str, record: dict) -> None:
        with self.registry._transaction() as conn:
            conn.execute(_EVENTS[kind].insert().values(run_id=run_id, record=record))

    def events(self, kind: str, run_id: str) -> list[dict]:
        table = _EVENTS[kind]
        with self.registry._transaction() as conn:
            return list(
                conn.execute(
                    select(table.c.record)
                    .where(table.c.run_id == run_id)
                    .order_by(table.c.sequence)
                ).scalars()
            )

    def recent_references(self, kind: str, limit: int = 8) -> list[dict]:
        with self.registry._transaction() as conn:
            table = _RECORDS[kind]
            columns = [column for column in table.columns if column.name != "record"]
            return [
                dict(row)
                for row in conn.execute(
                    select(*columns).order_by(literal_column("rowid").desc()).limit(limit)
                ).mappings()
            ]

    def register_specification_name(self, parse_run_id: str, name: str) -> None:
        with self.registry._transaction() as conn:
            conn.execute(
                insert(specification_names)
                .values(parse_run_id=parse_run_id, name=name)
                .on_conflict_do_nothing()
            )

    def specification_name(self, parse_run_id: str) -> str | None:
        with self.registry._transaction() as conn:
            return conn.scalar(
                select(specification_names.c.name).where(
                    specification_names.c.parse_run_id == parse_run_id
                )
            )

    def specification_ids(self) -> list[str]:
        with self.registry._transaction() as conn:
            return list(
                conn.scalars(
                    select(parse_runs.c.parse_run_id).order_by(literal_column("rowid").desc())
                )
            )

    def get_sheet_data(self, asset_id: str) -> bytes:
        with self.registry._transaction() as conn:
            data = conn.execute(
                select(sheets.c.data).where(sheets.c.asset_id == asset_id)
            ).scalar_one_or_none()
        if data is None:
            raise IraError("source_missing")
        return data

    def sheet_scope_metadata(self, import_id: str) -> dict:
        with self.registry._transaction() as conn:
            record = conn.scalar(
                select(func.json_remove(sheet_imports.c.record, "$.declarations")).where(
                    sheet_imports.c.import_id == import_id
                )
            )
        if record is None:
            raise IraError("not_found")
        return json.loads(record)

    def scoped_sheet_rows(self, import_id: str, rows: tuple[int, ...]) -> list[dict]:
        items = func.json_each(sheet_imports.c.record, "$.declarations").table_valued("value")
        row_id = func.json_extract(items.c.value, "$.row_id")
        query = (
            select(items.c.value)
            .select_from(sheet_imports.join(items, true()))
            .where(sheet_imports.c.import_id == import_id, row_id.in_(rows))
            .order_by(row_id)
        )
        with self.registry._transaction() as conn:
            return [json.loads(value) for value in conn.scalars(query)]

    def sheet_has_rows(self, import_id: str, rows: tuple[int, ...]) -> bool:
        items = func.json_each(sheet_imports.c.record, "$.declarations").table_valued("value")
        row_id = func.json_extract(items.c.value, "$.row_id")
        query = (
            select(func.count())
            .select_from(sheet_imports.join(items, true()))
            .where(sheet_imports.c.import_id == import_id, row_id.in_(rows))
        )
        with self.registry._transaction() as conn:
            return conn.scalar(query) == len(rows)

    def task_run_ids(self, draft_id: str) -> list[str]:
        with self.registry._transaction() as conn:
            return list(
                conn.scalars(
                    select(checks.c.run_id)
                    .where(checks.c.draft_id == draft_id)
                    .order_by(literal_column("rowid").desc())
                    .limit(50)
                )
            )

    def version_history(self, draft_id: str) -> list[dict]:
        with self.registry._transaction() as conn:
            return list(
                conn.scalars(
                    select(human_reviews.c.record)
                    .where(
                        human_reviews.c.draft_id == draft_id,
                        func.json_extract(human_reviews.c.record, "$.category") == "version",
                    )
                    .order_by(human_reviews.c.sequence)
                )
            )

    def human_history(self, draft_id: str, left_ref: str, right_ref: str) -> list[dict]:
        table = human_reviews
        with self.registry._transaction() as conn:
            return list(
                conn.execute(
                    select(table.c.record)
                    .where(
                        table.c.draft_id == draft_id,
                        table.c.left_ref == left_ref,
                        table.c.right_ref == right_ref,
                    )
                    .order_by(table.c.sequence)
                ).scalars()
            )

    def append_human_review(self, record: dict, previous_review_id: str) -> None:
        with self.registry._transaction() as conn:
            inserted = conn.execute(
                insert(human_reviews)
                .values(
                    draft_id=record["draft_id"],
                    left_ref=record["left_ref"],
                    right_ref=record["right_ref"],
                    previous_review_id=previous_review_id,
                    record=record,
                )
                .on_conflict_do_nothing()
            )
            if inserted.rowcount != 1:
                raise IraError("verification_stale")
