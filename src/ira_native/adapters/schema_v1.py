"""Initial registry schema. Kept stable for the first Alembic revision."""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

assets = Table(
    "source_assets",
    metadata,
    Column("asset_id", String(64), primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("media_type", String(32), nullable=False),
    Column("source_kind", String(16), nullable=False),
    Column("access_policy", JSON, nullable=False),
    Column("locator_internal", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("page_count", Integer),
    Column("asset_data", LargeBinary),
    UniqueConstraint("source_kind", "sha256"),
    CheckConstraint("source_kind IN ('pdf', 'parsed_json')"),
    CheckConstraint("size_bytes > 0"),
    CheckConstraint(
        "(source_kind = 'pdf' AND page_count IS NOT NULL AND page_count > 0) OR "
        "(source_kind = 'parsed_json' AND page_count IS NULL)"
    ),
    CheckConstraint("source_kind = 'pdf' OR asset_data IS NOT NULL"),
)
documents = Table(
    "documents",
    metadata,
    Column("document_id", Text, primary_key=True),
    Column("logical_title", Text),
)
revisions = Table(
    "document_revisions",
    metadata,
    Column("revision_id", Text, primary_key=True),
    Column("document_id", Text, ForeignKey("documents.document_id"), nullable=False),
    Column("asset_id", String(64), ForeignKey("source_assets.asset_id"), nullable=False),
    Column("revision_label", Text),
    Column("effective_from", Date),
    Column("scope", JSON),
    Column("relation_evidence", JSON, nullable=False),
    Index("ix_document_revisions_asset", "asset_id"),
)
runs = Table(
    "parse_runs",
    metadata,
    Column("parse_run_id", String(64), primary_key=True),
    Column("source_asset_id", String(64), ForeignKey("source_assets.asset_id"), nullable=False),
    Column("artifact_asset_id", String(64), ForeignKey("source_assets.asset_id"), nullable=False),
    Column("adapter_version", Text, nullable=False),
    Column("parser_version", Text),
    Column("parser_config", JSON),
    Column("binding_state", String(24), nullable=False),
    Column("binding_basis", JSON, nullable=False),
    UniqueConstraint("source_asset_id", "artifact_asset_id", "adapter_version"),
    CheckConstraint("source_asset_id <> artifact_asset_id"),
    CheckConstraint(
        "binding_state IN ('unknown', 'declared', 'manifest_attested', 'spot_checked', 'mismatch')"
    ),
)
evidence = Table(
    "evidence",
    metadata,
    Column("row_id", Integer, primary_key=True),
    Column("evidence_id", String(64), nullable=False, unique=True),
    Column("parse_run_id", String(64), ForeignKey("parse_runs.parse_run_id"), nullable=False),
    Column("source_pointer", Text, nullable=False),
    Column("page_index", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("search_text", Text, nullable=False),
    Column("record", JSON, nullable=False),
    UniqueConstraint("parse_run_id", "source_pointer"),
    CheckConstraint("page_index >= 0"),
    Index("ix_evidence_scope", "parse_run_id", "page_index", "kind"),
    Index("ix_evidence_exact", "search_text"),
)
