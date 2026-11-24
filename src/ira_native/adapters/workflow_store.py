"""SQLite persistence for verification records."""

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from ira_native.adapters.workflow_schema import (
    agent_runs,
    checks,
    drafts,
    reviews,
    sheet_imports,
    sheets,
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
