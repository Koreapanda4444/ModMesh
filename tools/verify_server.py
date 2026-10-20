from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from sqlalchemy import inspect
from sqlalchemy.engine import URL

from modmesh.apps.server.database import Database
from modmesh.apps.server.main import MAX_REQUEST_BYTES, create_app
from modmesh.core.config import ServerSettings


async def verify(url: str) -> None:
    database = Database(url)
    try:
        assert database.backend in {"postgresql", "sqlite"}
        database.migrate()
        revision = database.revision()
        database.migrate()
        assert database.revision() == revision
        tables = inspect(database.engine).get_table_names()
        assert "server_metadata" in tables and "alembic_version" in tables
        settings = ServerSettings(database_url=url)
        transport = httpx.ASGITransport(app=create_app(settings, database))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            health = await client.get("/health")
            assert health.status_code == 200 and health.json()["database"] == database.backend
            info = await client.get("/v1/info")
            assert info.status_code == 200 and info.json()["schema"] == revision
            response = await client.post(
                "/unknown", content=b"x", headers={"content-length": str(MAX_REQUEST_BYTES + 1)}
            )
            assert response.status_code == 413
        print(f"Server migrations, readiness and body bounds passed: {database.backend}")
    finally:
        database.close()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="modmesh-server-verification-") as directory:
        asyncio.run(
            verify(
                os.environ.get("MODMESH_TEST_DATABASE_URL")
                or str(URL.create("sqlite", database=str(Path(directory) / "server.sqlite3")))
            )
        )
