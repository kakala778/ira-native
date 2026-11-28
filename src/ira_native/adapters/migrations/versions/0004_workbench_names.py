"""Store local display names for uploaded specifications."""

from alembic import op

from ira_native.adapters.workflow_schema import specification_names

revision = "0004_workbench_names"
down_revision = "0003_human_verification"
branch_labels = None
depends_on = None


def upgrade():
    specification_names.create(op.get_bind())


def downgrade():
    raise NotImplementedError("This migration is forward-only.")
