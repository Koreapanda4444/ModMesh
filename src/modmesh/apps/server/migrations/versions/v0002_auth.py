import sqlalchemy as sa
from alembic import op

revision = "0002_auth"
down_revision = "0001_server"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table(
        "auth_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.String(320)),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])
    op.create_table(
        "rate_limits",
        sa.Column("bucket", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_rate_limits_expires_at", "rate_limits", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("Authentication data migrations are forward-only")
