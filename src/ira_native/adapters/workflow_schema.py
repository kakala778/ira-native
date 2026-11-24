"""Verification workflow tables for migration 0002."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, LargeBinary, MetaData, String, Table

from ira_native.adapters.schema_v1 import metadata as registry_metadata

metadata = MetaData()
registry_metadata.tables["parse_runs"].to_metadata(metadata)
sheets = Table(
    "spreadsheet_assets",
    metadata,
    Column("asset_id", String(64), primary_key=True),
    Column("record", JSON, nullable=False),
    Column("data", LargeBinary, nullable=False),
)
sheet_imports = Table(
    "spreadsheet_imports",
    metadata,
    Column("import_id", String(64), primary_key=True),
    Column("asset_id", ForeignKey("spreadsheet_assets.asset_id"), nullable=False),
    Column("record", JSON, nullable=False),
)
drafts = Table(
    "verification_drafts",
    metadata,
    Column("draft_id", String(64), primary_key=True),
    Column("parse_run_id", ForeignKey("parse_runs.parse_run_id"), nullable=False),
    Column("import_id", ForeignKey("spreadsheet_imports.import_id"), nullable=False),
    Column("record", JSON, nullable=False),
)
checks = Table(
    "verification_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("draft_id", ForeignKey("verification_drafts.draft_id"), nullable=False),
    Column("record", JSON, nullable=False),
)
reviews = Table(
    "review_records",
    metadata,
    Column("sequence", Integer, primary_key=True),
    Column("run_id", ForeignKey("verification_runs.run_id"), nullable=False, index=True),
    Column("record", JSON, nullable=False),
)
agent_runs = Table(
    "agent_runs",
    metadata,
    Column("sequence", Integer, primary_key=True),
    Column("run_id", ForeignKey("verification_runs.run_id"), nullable=False, index=True),
    Column("record", JSON, nullable=False),
)
TABLES = [sheets, sheet_imports, drafts, checks, reviews, agent_runs]
