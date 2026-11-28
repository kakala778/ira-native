"""Add append-only local human verification records."""

from alembic import op

from ira_native.adapters.workflow_schema import human_reviews

revision = "0003_human_verification"
down_revision = "0002_workflow"
branch_labels = None
depends_on = None


def upgrade():
    human_reviews.create(op.get_bind())


def downgrade():
    raise NotImplementedError("This migration is forward-only.")
