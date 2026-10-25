import sqlalchemy as sa
from alembic import op

revision = "0004_shares"
down_revision = "0003_packs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "share_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("packs.id"), nullable=False),
        sa.Column("revision", sa.Integer()),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("max_uses", sa.Integer()),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_share_codes_pack_id", "share_codes", ["pack_id"])


def downgrade() -> None:
    raise RuntimeError("Sharing data migrations are forward-only")
