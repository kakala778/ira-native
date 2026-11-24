"""Persist declaration sources, verification snapshots and append-only events."""

from alembic import op

from ira_native.adapters.workflow_schema import TABLES, metadata

revision = "0002_workflow"
down_revision = "0001_registry"
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind(), tables=TABLES)


def downgrade():
    raise NotImplementedError("This migration is forward-only.")
