from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Integer, MetaData, String, Table

metadata = MetaData()
users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("password_hash", String(512), nullable=False),
    Column("verified", Boolean, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column(
        "user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
)
auth_tokens = Table(
    "auth_tokens",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column(
        "user_id",
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("kind", String(16), nullable=False),
    Column("payload", String(320)),
    Column("expires_at", BigInteger, nullable=False),
)
rate_limits = Table(
    "rate_limits",
    metadata,
    Column("bucket", String(64), primary_key=True),
    Column("attempts", Integer, nullable=False),
    Column("expires_at", BigInteger, nullable=False, index=True),
)
