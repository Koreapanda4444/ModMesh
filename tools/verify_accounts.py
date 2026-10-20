from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from PySide6.QtWidgets import QApplication

from modmesh.apps.desktop.accounts import AccountPanel
from modmesh.apps.desktop.workers import Worker
from modmesh.apps.server.database import Database
from modmesh.apps.server.main import create_app
from modmesh.core.config import AppPaths, ClientSettings, ServerSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError, redact_text
from modmesh.remote.accounts import AccountClient
from modmesh.storage.metadata import MetadataStore


class Vault:
    def __init__(self):
        self.values = {}

    def get(self, server):
        return self.values.get(server)

    def set(self, server, value):
        if value is None:
            self.values.pop(server, None)
        else:
            self.values[server] = value


class Sink:
    ready = True

    def __init__(self):
        self.codes = []

    def send(self, kind, recipient, token):
        self.codes.append((kind, recipient, token))


def verify():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = QApplication.instance() or QApplication([])
    with TemporaryDirectory(prefix="modmesh-account-safety-") as directory:
        root = Path(directory)
        database = Database("sqlite:///" + str(root / "server.db"))
        database.migrate()
        sink, vault, seen = Sink(), Vault(), []
        app = create_app(ServerSettings(), database, sink)

        def dispatch(request):
            seen.append((request.url.host, request.headers.get("authorization")))

            async def call():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                    response = await client.request(
                        request.method,
                        str(request.url),
                        content=request.content,
                        headers=request.headers,
                    )
                    return httpx.Response(response.status_code, content=response.content)

            return asyncio.run(call())

        def factory(server):
            return AccountClient(server, vault, httpx.MockTransport(dispatch))

        client = factory("http://localhost")
        email, password = "desktop@example.com", "Desktop safe password phrase 123!"
        client.auth_action("register", {"email": email, "password": password})
        client.auth_action("verify", {"code": sink.codes[-1][2]})
        client.login(email, password, True)
        assert vault.get(client.server) and password not in json.dumps(vault.values)
        restored = factory("http://localhost/")
        assert restored.restore()["email"] == email
        isolated = factory("http://127.0.0.1")
        assert isolated.restore() is None
        isolated.request("GET", "/v1/info", authenticated=False)
        assert seen[-1] == ("127.0.0.1", None)
        token = json.loads(vault.get(client.server))["token"]
        assert token not in redact_text(token)
        restored.logout(True)
        assert not vault.values
        try:
            client.request("GET", "/v1/account")
        except ModMeshError as exc:
            assert exc.code == ErrorCode.AUTHENTICATION
        else:
            raise AssertionError("Revoked token accepted")
        assert client._token is None
        redirects = []

        def redirect(request):
            redirects.append(str(request.url))
            return httpx.Response(307, headers={"location": "https://other.example/v1/account"})

        redirected = AccountClient("https://first.example", vault, httpx.MockTransport(redirect))
        with contextlib.suppress(ModMeshError):
            redirected.request("GET", "/v1/info", authenticated=False)
        assert len(redirects) == 1
        workers = []

        def start(work, completed, failed=None, **kwargs):
            worker = Worker(work)
            worker.succeeded.connect(completed)
            worker.failed.connect(failed)
            workers.append(worker)
            worker.start()
            return worker

        def wait():
            deadline = time.monotonic() + 20
            quiet = 0
            while quiet < 10 and time.monotonic() < deadline:
                qt.processEvents()
                quiet = 0 if any(worker.isRunning() for worker in workers) else quiet + 1
                threading.Event().wait(0.01)
            assert not any(worker.isRunning() for worker in workers)

        settings = ClientSettings(AppPaths(root / "config", root / "data", root / "cache"))
        with (
            MetadataStore(settings.paths.database) as store,
            patch("modmesh.apps.desktop.accounts.AccountClient", factory),
        ):
            panel = AccountPanel(settings, store, start)
            panel.server.setText("http://localhost")
            panel.select_server()
            wait()
            assert panel.account is None
            panel.email.setText(email)
            panel.password.setText(password)
            panel.execute()
            wait()
            assert panel.account["email"] == email and not panel.password.text()
            panel.logout()
            wait()
            assert panel.account is None
            panel.server.setText("http://127.0.0.1")
            panel.select_server()
            wait()
            assert panel.account is None and store.get("desktop", "server")["url"]
            panel.close()
        database.close()
    print("Account UI, server isolation, token vault, revocation and redirect safety passed")


if __name__ == "__main__":
    verify()
