"""FastAPI bootstrap; business APIs are introduced in the server milestones."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from modmesh import __version__
from modmesh.core.config import ConfigurationError, load_server_settings


def create_app() -> FastAPI:
    app = FastAPI(title="ModMesh Server", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "modmesh-server", "version": __version__}

    return app


async def check() -> dict[str, str]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health")
        response.raise_for_status()
        result: dict[str, str] = response.json()
        if result.get("status") != "ok":
            raise RuntimeError("Server health check failed")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="ModMesh server")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="Server TOML configuration")
    parser.add_argument("--check", action="store_true", help="Exercise the ASGI health endpoint")
    args = parser.parse_args()
    try:
        settings = load_server_settings(args.config)
    except ConfigurationError as exc:
        parser.error(str(exc))
    if args.check:
        print(json.dumps(asyncio.run(check())))
    else:
        uvicorn.run(create_app(), host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
