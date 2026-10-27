from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from sqlalchemy import (
    Column,
    Connection,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import ConnectionPoolEntry

from modmesh.core.diagnostics import ErrorCode, ModMeshError

_schema = MetaData()
_migrations = Table(
    "schema_migrations",
    _schema,
    Column("version", Integer, primary_key=True),
    Column("applied_at", String, nullable=False),
)
_records = Table(
    "metadata_records",
    _schema,
    Column("namespace", String(128), primary_key=True),
    Column("record_key", String(1024), primary_key=True),
    Column("payload", Text, nullable=False),
    Column("updated_at", String, nullable=False),
)
SCHEMA_VERSION = 1


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _configure_sqlite(connection: sqlite3.Connection, record: ConnectionPoolEntry) -> None:
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")


def _validate_key(namespace: str, key: str) -> None:
    if not namespace or len(namespace) > 128 or not key or len(key) > 1024:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid metadata namespace or key")


def _decode(payload: str) -> dict[str, object]:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ModMeshError(ErrorCode.STORAGE, "Invalid stored metadata") from exc
    if not isinstance(data, dict):
        raise ModMeshError(ErrorCode.STORAGE, "Stored metadata must be an object")
    return dict(data)


class MetadataTransaction:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get(self, namespace: str, key: str) -> dict[str, object] | None:
        _validate_key(namespace, key)
        payload = self._connection.execute(
            select(_records.c.payload).where(
                _records.c.namespace == namespace, _records.c.record_key == key
            )
        ).scalar_one_or_none()
        return None if payload is None else _decode(payload)

    def put(self, namespace: str, key: str, payload: Mapping[str, object]) -> None:
        _validate_key(namespace, key)
        try:
            encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ModMeshError(ErrorCode.VALIDATION, "Metadata must be valid JSON") from exc
        statement = insert(_records).values(
            namespace=namespace, record_key=key, payload=encoded, updated_at=_timestamp()
        )
        self._connection.execute(
            statement.on_conflict_do_update(
                index_elements=[_records.c.namespace, _records.c.record_key],
                set_={
                    "payload": statement.excluded.payload,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    def delete(self, namespace: str, key: str) -> bool:
        _validate_key(namespace, key)
        result = self._connection.execute(
            _records.delete().where(_records.c.namespace == namespace, _records.c.record_key == key)
        )
        return result.rowcount > 0

    def list(self, namespace: str) -> list[tuple[str, dict[str, object]]]:
        _validate_key(namespace, "_")
        rows = self._connection.execute(
            select(_records.c.record_key, _records.c.payload)
            .where(_records.c.namespace == namespace)
            .order_by(_records.c.record_key)
        )
        return [(str(row.record_key), _decode(row.payload)) for row in rows]


class MetadataStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            self._engine = create_engine(
                URL.create("sqlite", database=str(self.path)), connect_args={"timeout": 5}
            )
            event.listen(self._engine, "connect", _configure_sqlite)
            self._migrate()
        except (OSError, SQLAlchemyError, ModMeshError) as exc:
            if hasattr(self, "_engine"):
                self._engine.dispose()
            if isinstance(exc, ModMeshError):
                raise
            raise ModMeshError(ErrorCode.STORAGE, "Cannot initialize local metadata") from exc

    def _migrate(self) -> None:
        with self._engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _migrations.create(connection, checkfirst=True)
                versions = list(connection.execute(select(_migrations.c.version)).scalars())
                if sorted(versions) != list(range(1, max(versions, default=0) + 1)) or any(
                    v > SCHEMA_VERSION for v in versions
                ):
                    raise ModMeshError(
                        ErrorCode.STORAGE, "Unsupported local metadata schema version"
                    )
                if not versions:
                    _records.create(connection)
                    connection.execute(
                        _migrations.insert().values(version=1, applied_at=_timestamp())
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @property
    def schema_version(self) -> int:
        with self._engine.connect() as connection:
            return int(connection.execute(select(_migrations.c.version)).scalar_one())

    def backup(self, destination: Path) -> Path:
        destination = destination.expanduser().absolute()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Backup requires a new writable file") from exc
        os.close(descriptor)
        try:
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ModMeshError(ErrorCode.INTEGRITY, "Metadata backup is inconsistent")
                finally:
                    target.close()
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
        except (OSError, sqlite3.Error, ModMeshError) as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, ModMeshError):
                raise
            raise ModMeshError(
                ErrorCode.STORAGE, "Cannot create consistent metadata backup"
            ) from exc
        return destination

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[MetadataTransaction]:
        try:
            with self._engine.begin() as connection:
                if write:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield MetadataTransaction(connection)
        except SQLAlchemyError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Local metadata transaction failed") from exc

    def get(self, namespace: str, key: str) -> dict[str, object] | None:
        with self.transaction() as transaction:
            return transaction.get(namespace, key)

    def put(self, namespace: str, key: str, payload: Mapping[str, object]) -> None:
        with self.transaction(write=True) as transaction:
            transaction.put(namespace, key, payload)

    def list(self, namespace: str) -> list[tuple[str, dict[str, object]]]:
        with self.transaction() as transaction:
            return transaction.list(namespace)

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
