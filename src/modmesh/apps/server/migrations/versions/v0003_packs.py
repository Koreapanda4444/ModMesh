import sqlalchemy as sa
from alembic import op

revision = "0003_packs"
down_revision = "0002_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "packs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_packs_owner_id", "packs", ["owner_id"])
    op.create_table(
        "pack_versions",
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("packs.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Published pack history migrations are forward-only")
