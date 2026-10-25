from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

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
packs = Table(
    "packs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("owner_id", String(36), ForeignKey("users.id"), nullable=False, index=True),
    Column("name", String(100), nullable=False),
    Column("description", String(2000), nullable=False),
    Column("visibility", String(16), nullable=False),
    Column("latest_revision", Integer, nullable=False),
    Column("archived", Boolean, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)
pack_versions = Table(
    "pack_versions",
    metadata,
    Column("pack_id", String(36), ForeignKey("packs.id"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("manifest_json", Text, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
share_codes = Table(
    "share_codes",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("code_hash", String(64), nullable=False, unique=True),
    Column("pack_id", String(36), ForeignKey("packs.id"), nullable=False, index=True),
    Column("revision", Integer),
    Column("expires_at", BigInteger, nullable=False),
    Column("max_uses", Integer),
    Column("uses", Integer, nullable=False),
    Column("revoked", Boolean, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
