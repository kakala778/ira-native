"""Create sources, revisions, parse runs, evidence and the optional trigram index."""

from alembic import op
from sqlalchemy.exc import OperationalError

from ira_native.adapters.schema_v1 import metadata

revision = "0001_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    metadata.create_all(conn)
    try:
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE evidence_fts USING fts5("
            "search_text, content='evidence', content_rowid='row_id', "
            "tokenize='trigram case_sensitive 1')"
        )
    except OperationalError as error:
        message = str(error.orig).lower()
        if not ("no such module: fts5" in message or "no such tokenizer" in message):
            raise
        return
    conn.exec_driver_sql("""
        CREATE TRIGGER evidence_index_insert AFTER INSERT ON evidence BEGIN
            INSERT INTO evidence_fts(rowid, search_text) VALUES (new.row_id, new.search_text);
        END
    """)


def downgrade() -> None:
    raise NotImplementedError("This migration is forward-only.")
