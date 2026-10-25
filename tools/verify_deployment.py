from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from modmesh.core.config import load_server_settings


def port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def wait_health(number, backend):
    for _ in range(100):
        try:
            response = httpx.get(f"http://127.0.0.1:{number}/health", timeout=1)
            if response.status_code == 200:
                assert response.json()["database"] == backend
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Deployed server did not become ready")


def verify(container):
    root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="modmesh-deployment-") as directory:
        temporary = Path(directory)
        secret = temporary / "database-password"
        secret.write_text(secrets.token_urlsafe(32))
        secret.chmod(0o444)
        configured = load_server_settings(
            env={
                "MODMESH_DATABASE_URL": "postgresql+psycopg://modmesh@localhost/modmesh",
                "MODMESH_DATABASE_PASSWORD_FILE": str(secret),
            }
        )
        assert secret.read_text() not in repr(configured)
        assert (
            load_server_settings(env={"MODMESH_SMTP_HOST": "", "MODMESH_SMTP_FROM": ""}).smtp_host
            is None
        )
        number = port()
        environment = {
            **os.environ,
            "MODMESH_ENV": "development",
            "MODMESH_BIND_HOST": "127.0.0.1",
            "MODMESH_BIND_PORT": str(number),
            "MODMESH_DATABASE_URL": "sqlite:///" + str(temporary / "server.db"),
        }
        for key in ("MODMESH_DATABASE_PASSWORD", "MODMESH_DATABASE_PASSWORD_FILE"):
            environment.pop(key, None)
        if container:
            environment.update(
                {"MODMESH_DB_PASSWORD_FILE": str(secret), "MODMESH_SERVER_PORT": str(number)}
            )
            command = [
                "docker",
                "compose",
                "-p",
                "modmesh-check-" + uuid4().hex[:12],
                "-f",
                str(root / "compose.yml"),
            ]
            try:
                subprocess.run(
                    [*command, "up", "--build", "--wait", "--wait-timeout", "180"],
                    env=environment,
                    check=True,
                )
                wait_health(number, "postgresql")
                subprocess.run([*command, "run", "--rm", "migrate"], env=environment, check=True)
            except subprocess.CalledProcessError:
                subprocess.run(
                    [*command, "logs", "--no-color", "--tail=100"], env=environment, check=False
                )
                raise
            finally:
                subprocess.run(
                    [*command, "down", "-v", "--remove-orphans"], env=environment, check=True
                )
        else:
            command = [sys.executable, "-m", "modmesh.apps.server.main"]
            subprocess.run([*command, "--migrate"], env=environment, check=True)
            process = subprocess.Popen(
                command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                wait_health(number, "sqlite")
                response = httpx.get(f"http://127.0.0.1:{number}/v1/info", timeout=5)
                assert response.status_code == 200 and response.json()["schema"]
            finally:
                process.terminate()
                process.wait(timeout=10)
    print("Container deployment passed" if container else "Standalone deployment passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", action="store_true")
    verify(parser.parse_args().container)
