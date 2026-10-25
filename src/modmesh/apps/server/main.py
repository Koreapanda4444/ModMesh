from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import URL
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from modmesh import __version__
from modmesh.apps.server.auth import Authentication
from modmesh.apps.server.auth_routes import router as auth_router
from modmesh.apps.server.database import Database
from modmesh.apps.server.mail import Mailer, SMTPMailer
from modmesh.apps.server.pack_routes import router as pack_router
from modmesh.apps.server.share_routes import router as share_router
from modmesh.core.config import ConfigurationError, ServerSettings, load_server_settings
from modmesh.core.diagnostics import ErrorCode, ModMeshError, configure_logging

MAX_REQUEST_BYTES = 3 * 1024**2


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"error": "Invalid content length"}, status_code=400)(
                scope, receive, send
            )
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            await JSONResponse({"error": "Request body is too large"}, status_code=413)(
                scope, receive, send
            )
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Request body is too large")
            return message

        await self.app(scope, limited, send)


def create_app(
    settings: ServerSettings | None = None,
    database: Database | None = None,
    mailer: Mailer | None = None,
) -> FastAPI:
    settings = settings or load_server_settings()
    owned = database is None and settings.database_url is not None
    database = database or (Database(settings.database_url) if settings.database_url else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned and database:
                database.close()

    app = FastAPI(
        title="ModMesh Server",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(BodyLimit)
    app.state.database = database
    app.state.settings = settings
    app.state.authentication = (
        Authentication(database, settings, mailer or SMTPMailer(settings)) if database else None
    )
    app.include_router(auth_router)
    app.include_router(pack_router)
    app.include_router(share_router)

    @app.middleware("http")
    async def no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError) -> JSONResponse:
        errors = [{"location": item["loc"], "type": item["type"]} for item in error.errors()]
        return JSONResponse(
            {
                "error": {
                    "code": "validation",
                    "message": "Invalid request fields",
                    "fields": errors,
                }
            },
            status_code=422,
        )

    @app.exception_handler(ModMeshError)
    async def application_error(request: Request, error: ModMeshError) -> JSONResponse:
        logging.getLogger("modmesh.server").warning(
            "Request failed", extra={"context": error.as_dict()}
        )
        status = {
            ErrorCode.NOT_FOUND: 404,
            ErrorCode.AUTHENTICATION: 401,
            ErrorCode.RATE_LIMIT: 429,
            ErrorCode.VALIDATION: 422,
            ErrorCode.CONFLICT: 409,
            ErrorCode.UNSUPPORTED: 422,
            ErrorCode.STORAGE: 503,
        }.get(error.code, 500)
        return JSONResponse(
            status_code=status,
            content={"error": error.as_dict()},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        if database:
            database.revision()
        return {
            "status": "ok",
            "service": "modmesh-server",
            "version": __version__,
            "database": database.backend if database else "disabled",
        }

    @app.get("/v1/info")
    def info() -> dict[str, object]:
        return {
            "api_version": 1,
            "version": __version__,
            "database": database.backend if database else "disabled",
            "schema": database.revision() if database else None,
        }

    return app


async def check(settings: ServerSettings | None = None) -> dict[str, str]:
    settings = settings or ServerSettings()
    with TemporaryDirectory(prefix="modmesh-server-check-") as directory:
        configured = settings.database_url is not None
        database = Database(
            settings.database_url
            or str(URL.create("sqlite", database=str(Path(directory) / "check.sqlite3")))
        )
        try:
            if not configured:
                database.migrate()
            transport = httpx.ASGITransport(app=create_app(settings, database))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                response = await client.get("/health")
                response.raise_for_status()
                result: dict[str, str] = response.json()
                if result.get("status") != "ok":
                    raise RuntimeError("Server health check failed")
                info = await client.get("/v1/info")
                assert info.status_code == 200 and info.json()["schema"]
                return result
        finally:
            database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="ModMesh server")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="Server TOML configuration")
    parser.add_argument("--check", action="store_true", help="Exercise database-backed ASGI health")
    parser.add_argument("--migrate", action="store_true", help="Apply reviewed server migrations")
    args = parser.parse_args()
    try:
        settings = load_server_settings(args.config)
    except ConfigurationError as exc:
        parser.error(str(exc))
    logger = configure_logging(level=settings.log_level)
    logger.info("Server starting", extra={"context": {"version": __version__}})
    if args.migrate:
        if not settings.database_url:
            parser.error("Server migration requires MODMESH_DATABASE_URL")
        database = Database(settings.database_url)
        try:
            database.migrate()
            print(json.dumps({"schema": database.revision(), "status": "migrated"}))
        finally:
            database.close()
    elif args.check:
        print(json.dumps(asyncio.run(check(settings))))
    else:
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
