from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping

from modmesh.apps.server.database import Database
from modmesh.apps.server.mail import Mailer, normalize_email
from modmesh.apps.server.schema import auth_tokens, rate_limits, sessions, users
from modmesh.core.config import ServerSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def token_hash(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ModMeshError(ErrorCode.AUTHENTICATION, "Invalid or expired authentication code")
    return digest(value)


@dataclass(frozen=True)
class Account:
    user_id: str
    email: str
    verified: bool
    created_at: int
    password_hash: str = field(repr=False)

    @classmethod
    def read(cls, row: RowMapping) -> Account:
        return cls(
            str(row["id"]),
            str(row["email"]),
            row["verified"] is True,
            int(str(row["created_at"])),
            str(row["password_hash"]),
        )

    def document(self) -> dict[str, object]:
        return {
            "id": self.user_id,
            "email": self.email,
            "verified": self.verified,
            "created_at": self.created_at,
        }


def require_owner(account: Account, owner_id: str) -> None:
    if account.user_id != owner_id:
        raise ModMeshError(ErrorCode.NOT_FOUND, "Resource is unavailable")


class Authentication:
    def __init__(
        self,
        database: Database,
        settings: ServerSettings,
        mailer: Mailer,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database, self.settings, self.mailer, self.clock = database, settings, mailer, clock
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
        self.slots = threading.BoundedSemaphore(2)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    @contextmanager
    def hash_slot(self) -> Iterator[None]:
        if not self.slots.acquire(timeout=3):
            raise ModMeshError(ErrorCode.RATE_LIMIT, "Password service is busy; retry later")
        try:
            yield
        finally:
            self.slots.release()

    def hash_password(self, password: str) -> str:
        if not 15 <= len(password) <= 128 or not password.strip():
            raise ModMeshError(ErrorCode.VALIDATION, "Password needs 15–128 characters")
        with self.hash_slot():
            return self.hasher.hash(password)

    def matches(self, password_hash: str, password: str) -> bool:
        with self.hash_slot():
            try:
                return self.hasher.verify(password_hash, password)
            except (VerificationError, InvalidHashError):
                return False

    def limit(self, scope: str, subject: str, maximum: int, seconds: int = 300) -> None:
        now = int(self.clock())
        bucket = digest(f"{scope}:{subject}:{now // seconds}")
        insertion = postgres_insert if self.database.backend == "postgresql" else sqlite_insert
        statement = insertion(rate_limits).values(
            bucket=bucket, attempts=1, expires_at=(now // seconds + 1) * seconds
        )
        count_statement = statement.on_conflict_do_update(
            index_elements=[rate_limits.c.bucket],
            set_={"attempts": rate_limits.c.attempts + 1},
            where=rate_limits.c.attempts < maximum,
        ).returning(rate_limits.c.attempts)
        with self.database.transaction() as connection:
            if connection.execute(count_statement).scalar_one_or_none() is None:
                raise ModMeshError(ErrorCode.RATE_LIMIT, "Too many requests; retry later")
            connection.execute(rate_limits.delete().where(rate_limits.c.expires_at < now - 86400))

    def ensure_mail(self) -> None:
        if not self.mailer.ready:
            raise ModMeshError(ErrorCode.STORAGE, "Configure email delivery on this server")

    def issue_code(
        self, connection: Connection, user_id: str, kind: str, payload: str | None = None
    ) -> str:
        token = secrets.token_urlsafe(32)
        connection.execute(
            auth_tokens.delete().where(auth_tokens.c.user_id == user_id, auth_tokens.c.kind == kind)
        )
        connection.execute(
            auth_tokens.insert().values(
                token_hash=digest(token),
                user_id=user_id,
                kind=kind,
                payload=payload,
                expires_at=int(self.clock()) + (86400 if kind == "verify" else 3600),
            )
        )
        return token

    def consume_code(self, connection: Connection, code: str, kind: str) -> tuple[str, str | None]:
        row = (
            connection.execute(
                auth_tokens.delete()
                .where(
                    auth_tokens.c.token_hash == token_hash(code),
                    auth_tokens.c.kind == kind,
                    auth_tokens.c.expires_at > int(self.clock()),
                )
                .returning(auth_tokens.c.user_id, auth_tokens.c.payload)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ModMeshError(ErrorCode.AUTHENTICATION, "Invalid or expired authentication code")
        return str(row["user_id"]), str(row["payload"]) if row["payload"] is not None else None

    def register(self, email: str, password: str, peer: str) -> None:
        email = normalize_email(email)
        self.limit("register-ip", peer, 6, 3600)
        self.limit("register-email", email, 3, 3600)
        self.ensure_mail()
        password_hash = self.hash_password(password)
        token = None
        with self.database.transaction() as connection:
            insertion = postgres_insert if self.database.backend == "postgresql" else sqlite_insert
            statement = insertion(users).values(
                id=str(uuid4()),
                email=email,
                password_hash=password_hash,
                verified=False,
                created_at=int(self.clock()),
            )
            connection.execute(statement.on_conflict_do_nothing(index_elements=[users.c.email]))
            row = (
                connection.execute(select(users).where(users.c.email == email).with_for_update())
                .mappings()
                .one()
            )
            if row["verified"] is not True:
                token = self.issue_code(connection, str(row["id"]), "verify")
        if token:
            self.mailer.send("verify", email, token)

    def request_code(self, email: str, kind: str, peer: str) -> None:
        email = normalize_email(email)
        self.limit(kind + "-ip", peer, 10, 3600)
        self.limit(kind + "-email", email, 3, 3600)
        self.ensure_mail()
        token = None
        with self.database.transaction() as connection:
            row = (
                connection.execute(select(users).where(users.c.email == email).with_for_update())
                .mappings()
                .one_or_none()
            )
            if row is not None and (kind != "verify" or row["verified"] is not True):
                token = self.issue_code(connection, str(row["id"]), kind)
        if token:
            self.mailer.send(kind, email, token)

    def confirm_email(self, code: str, peer: str) -> None:
        self.limit("verify-code", peer, 30)
        with self.database.transaction() as connection:
            user_id, _ = self.consume_code(connection, code, "verify")
            connection.execute(users.update().where(users.c.id == user_id).values(verified=True))

    def login(self, email: str, password: str, peer: str) -> dict[str, object]:
        email = normalize_email(email)
        self.limit("login-ip", peer, 30)
        self.limit("login-email", email, 10)
        with self.database.transaction() as connection:
            row = (
                connection.execute(select(users).where(users.c.email == email).with_for_update())
                .mappings()
                .one_or_none()
            )
            account = Account.read(row) if row else None
            valid = self.matches(account.password_hash if account else self.dummy_hash, password)
            if account is None or not valid or not account.verified:
                raise ModMeshError(
                    ErrorCode.AUTHENTICATION, "Invalid credentials or email verification required"
                )
            if self.hasher.check_needs_rehash(account.password_hash):
                connection.execute(
                    users.update()
                    .where(users.c.id == account.user_id)
                    .values(password_hash=self.hash_password(password))
                )
            now = int(self.clock())
            connection.execute(
                sessions.delete().where(
                    sessions.c.user_id == account.user_id, sessions.c.expires_at <= now
                )
            )
            existing = (
                connection.execute(
                    select(sessions.c.token_hash)
                    .where(sessions.c.user_id == account.user_id)
                    .order_by(sessions.c.created_at)
                )
                .scalars()
                .all()
            )
            for value in existing[: max(0, len(existing) - 9)]:
                connection.execute(sessions.delete().where(sessions.c.token_hash == value))
            token = secrets.token_urlsafe(32)
            expires = now + self.settings.session_seconds
            connection.execute(
                sessions.insert().values(
                    token_hash=digest(token),
                    user_id=account.user_id,
                    created_at=now,
                    expires_at=expires,
                )
            )
            return {
                "access_token": token,
                "token_type": "Bearer",
                "expires_at": expires,
                "account": account.document(),
            }

    def authenticate(self, authorization: str | None) -> Account:
        parts = (authorization or "").split(" ")
        if len(parts) != 2 or parts[0].casefold() != "bearer":
            raise ModMeshError(ErrorCode.AUTHENTICATION, "Authentication is required")
        hashed = token_hash(parts[1])
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(users)
                    .join(sessions, sessions.c.user_id == users.c.id)
                    .where(
                        sessions.c.token_hash == hashed,
                        sessions.c.expires_at > int(self.clock()),
                        users.c.verified.is_(True),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ModMeshError(ErrorCode.AUTHENTICATION, "Session is expired or revoked")
            return Account.read(row)

    def logout(self, authorization: str, *, all_sessions: bool = False) -> None:
        account = self.authenticate(authorization)
        with self.database.transaction() as connection:
            condition = (
                sessions.c.user_id == account.user_id
                if all_sessions
                else sessions.c.token_hash == token_hash(authorization.split(" ")[1])
            )
            connection.execute(sessions.delete().where(condition))

    def reset_password(self, code: str, password: str, peer: str) -> None:
        self.limit("reset-code", peer, 20)
        with self.database.transaction() as connection:
            user_id, _ = self.consume_code(connection, code, "reset")
            password_hash = self.hash_password(password)
            connection.execute(
                users.update().where(users.c.id == user_id).values(password_hash=password_hash)
            )
            connection.execute(sessions.delete().where(sessions.c.user_id == user_id))
            connection.execute(auth_tokens.delete().where(auth_tokens.c.user_id == user_id))

    def change_password(self, authorization: str, current: str, password: str, peer: str) -> None:
        account = self.authenticate(authorization)
        self.limit("change-password", account.user_id + peer, 10)
        password_hash = self.hash_password(password)
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(users).where(users.c.id == account.user_id).with_for_update()
                )
                .mappings()
                .one()
            )
            if not self.matches(str(row["password_hash"]), current):
                raise ModMeshError(ErrorCode.AUTHENTICATION, "Current password is incorrect")
            connection.execute(
                users.update()
                .where(users.c.id == account.user_id)
                .values(password_hash=password_hash)
            )
            connection.execute(sessions.delete().where(sessions.c.user_id == account.user_id))
            connection.execute(auth_tokens.delete().where(auth_tokens.c.user_id == account.user_id))

    def request_email_change(
        self, authorization: str, email: str, password: str, peer: str
    ) -> None:
        account = self.authenticate(authorization)
        email = normalize_email(email)
        self.limit("change-email", account.user_id + peer, 3, 3600)
        self.ensure_mail()
        token = None
        with self.database.transaction() as connection:
            current = connection.execute(
                select(users.c.password_hash).where(users.c.id == account.user_id).with_for_update()
            ).scalar_one()
            if not self.matches(str(current), password):
                raise ModMeshError(ErrorCode.AUTHENTICATION, "Current password is incorrect")
            if (
                connection.execute(
                    select(users.c.id).where(users.c.email == email)
                ).scalar_one_or_none()
                is None
            ):
                token = self.issue_code(connection, account.user_id, "email", email)
        if token:
            self.mailer.send("email", email, token)

    def confirm_email_change(self, code: str, peer: str) -> None:
        self.limit("email-code", peer, 20)
        with self.database.transaction() as connection:
            user_id, email = self.consume_code(connection, code, "email")
            if email is None:
                raise ModMeshError(ErrorCode.AUTHENTICATION, "Invalid authentication code")
            connection.execute(
                users.update().where(users.c.id == user_id).values(email=email, verified=True)
            )
            connection.execute(sessions.delete().where(sessions.c.user_id == user_id))
            connection.execute(auth_tokens.delete().where(auth_tokens.c.user_id == user_id))
