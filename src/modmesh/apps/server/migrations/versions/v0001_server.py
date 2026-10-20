import sqlalchemy as sa
from alembic import op

revision = "0001_server"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "server_metadata",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(256), nullable=False),
    )
    op.bulk_insert(table, [{"key": "application", "value": "ModMesh"}])


def downgrade() -> None:
    raise RuntimeError(
        "Server data migrations are forward-only; restore a reviewed database backup"
    )
