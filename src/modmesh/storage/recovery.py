import os
import sqlite3
from contextlib import closing
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.storage.metadata import SCHEMA_VERSION, MetadataStore


def restore_metadata(backup: Path, destination: Path) -> Path:
    backup = backup.expanduser().absolute()
    if backup.is_symlink() or backup.is_junction() or not backup.is_file():
        raise ModMeshError(ErrorCode.VALIDATION, "Select a regular metadata backup")
    try:
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as source:
            versions = sorted(
                row[0] for row in source.execute("SELECT version FROM schema_migrations")
            )
            if versions != list(range(1, SCHEMA_VERSION + 1)):
                raise ModMeshError(
                    ErrorCode.STORAGE, "Unsupported backup schema; original retained"
                )
            if source.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ModMeshError(ErrorCode.INTEGRITY, "Backup integrity check failed")
            source.execute("SELECT namespace, record_key, payload FROM metadata_records LIMIT 1")
    except sqlite3.Error as exc:
        raise ModMeshError(ErrorCode.STORAGE, "Invalid metadata backup; original retained") from exc
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ModMeshError(
            ErrorCode.CONFLICT, "Restore to a new path; preserve the current database"
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        with MetadataStore(destination) as store:
            if store.schema_version != SCHEMA_VERSION:
                raise ModMeshError(ErrorCode.STORAGE, "Restored schema does not match")
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
    except (OSError, sqlite3.Error, ModMeshError):
        destination.unlink(missing_ok=True)
        raise
    return destination
