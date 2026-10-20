from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.storage.metadata import _configure_sqlite


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        if parsed.get_backend_name() == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        elif parsed.get_backend_name() != "sqlite":
            raise ModMeshError(ErrorCode.CONFIGURATION, "Unsupported server database")
        self.engine = create_engine(
            parsed,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 5}
            if parsed.get_backend_name() == "postgresql"
            else {},
        )
        self.backend = parsed.get_backend_name()
        if self.backend == "sqlite":
            event.listen(self.engine, "connect", _configure_sqlite)

    def config(self) -> Config:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        return config

    def migrate(self) -> None:
        try:
            with self.engine.begin() as connection:
                if self.backend == "postgresql":
                    connection.execute(text("SELECT pg_advisory_xact_lock(66427519)"))
                else:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                config = self.config()
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        except SQLAlchemyError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Server migration failed") from exc

    def revision(self) -> str:
        try:
            with self.engine.connect() as connection:
                current = MigrationContext.configure(connection).get_current_revision()
                expected = ScriptDirectory.from_config(self.config()).get_current_head()
                if current is None or current != expected:
                    raise ModMeshError(
                        ErrorCode.STORAGE, "Run server migrations before serving requests"
                    )
                connection.execute(
                    text("SELECT value FROM server_metadata WHERE key = 'application'")
                )
                return current
        except SQLAlchemyError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Server database is unavailable") from exc

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        try:
            with self.engine.begin() as connection:
                if self.backend == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield connection
        except IntegrityError as exc:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Server record conflicts with existing data"
            ) from exc
        except SQLAlchemyError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Server database transaction failed") from exc

    def close(self) -> None:
        self.engine.dispose()
