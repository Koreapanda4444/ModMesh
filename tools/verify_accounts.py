from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from PySide6.QtWidgets import QApplication

from modmesh.adapters.games.ark_evolved import EXECUTABLE, MODS, ArkEvolvedAdapter
from modmesh.apps.desktop.accounts import AccountPanel
from modmesh.apps.desktop.custom import CustomPanel
from modmesh.apps.desktop.packs import PacksPanel
from modmesh.apps.desktop.publishing import PublishingPanel
from modmesh.apps.desktop.rules import RulesPanel
from modmesh.apps.desktop.search import SearchPanel
from modmesh.apps.desktop.workers import Worker
from modmesh.apps.desktop.workshop import WorkshopPanel
from modmesh.apps.runtime import create_game_registry
from modmesh.apps.server.database import Database
from modmesh.apps.server.main import create_app
from modmesh.core.config import AppPaths, ClientSettings, ServerSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError, redact_text
from modmesh.core.games import DiscoveryContext, GameRegistry
from modmesh.core.packs import PackContext, PackManifest
from modmesh.domain.models import GameInstallation, Platform
from modmesh.remote.accounts import AccountClient
from modmesh.remote.sync import RemotePacks
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
            online = PublishingPanel(panel, start)
            manifest = PackManifest.create(
                "Desktop published pack",
                PackContext("minecraft", "java", Platform.WINDOWS, "1.21.1", None, None),
                (),
            )
            path = root / "publish.json"
            path.write_bytes(manifest.dumps())
            online.path.setText(str(path))
            online.publish()
            wait()
            assert len(online.records) == 1, online.status.text()
            online.list.selectRow(0)
            wait()
            assert online.revisions[0]["digest"] == manifest.digest
            online.version_list.selectRow(0)
            online.preview_version()
            wait()
            assert manifest.pack_id in online.preview.toPlainText()
            newer = replace(manifest, name="Desktop v2", revision=2)
            path.write_bytes(newer.dumps())
            online.publish(True)
            wait()
            assert online.records[0]["latest_revision"] == 2
            online.list.selectRow(0)
            wait()
            assert {row["revision"] for row in online.revisions} == {1, 2}
            online.description.setText("Updated from desktop")
            online.edit()
            wait()
            assert online.records[0]["description"] == "Updated from desktop"
            online.list.selectRow(0)
            wait()
            online.visibility.setCurrentIndex(online.visibility.findData("unlisted"))
            online.edit()
            wait()
            online.list.selectRow(0)
            wait()
            online.issue_share()
            wait()
            assert online.share_code.text().startswith("MM-"), online.status.text()
            assert online.share_list.count() == 1
            code = online.share_code.text()
            guest = factory("http://localhost")
            remote = RemotePacks(guest, store)
            snapshot = remote.fetch(code)
            assert snapshot.manifest.digest == newer.digest
            remote.applied("installation:profile", snapshot)
            assert remote.receipt("installation:profile").digest == newer.digest
            assert (
                RemotePacks(factory("http://127.0.0.1"), store).receipt("installation:profile")
                is None
            )
            assert remote.fetch(code, previous=newer).diff.added == ()
            try:
                remote.fetch(code, previous=replace(newer, revision=3))
            except ModMeshError as exc:
                assert exc.code == ErrorCode.CONFLICT
            else:
                raise AssertionError("Remote rollback was accepted")
            importer = PacksPanel(settings, GameRegistry(), start, lambda: None)
            importer.bind_accounts(panel)
            importer.remote_code.setText(code)
            importer.fetch_remote()
            wait()
            assert importer.manifest.digest == newer.digest
            importer.server_changed("other server")
            assert importer.manifest is None and importer.prepared is None
            importer.close()
            online.revoke_share()
            wait()
            assert "True" in online.share_list.currentText()
            panel.logout()
            wait()
            assert panel.account is None
            assert not online.records and not online.preview.toPlainText()
            online.close()
            custom = CustomPanel(settings, start)
            custom.set_context(
                GameInstallation("palworld", root / "game", "steam", Platform.WINDOWS)
            )
            wait()
            local = root / "custom.pak"
            local.write_bytes(b"registered custom file")
            custom.name.setText("Custom UI")
            custom.location.setText(str(local))
            custom.register()
            wait()
            assert custom.list.rowCount() == 1, custom.status.text()
            custom.list.selectRow(0)
            custom.remove()
            wait()
            assert custom.list.rowCount() == 0 and local.exists()
            custom.close()
            ark = root / "steam/steamapps/common/ARK"
            binary = ark / EXECUTABLE
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"MZ")
            (ark.parent.parent / "appmanifest_346110.acf").write_text(
                '"AppState" {"appid" "346110" "installdir" "ARK" "buildid" "123"}'
            )
            (ark / MODS).mkdir(parents=True)
            (ark / MODS / "123.mod").write_bytes(b"descriptor")
            workshop = WorkshopPanel(start)
            workshop.set_context(ArkEvolvedAdapter().inspect(ark, Platform.WINDOWS))
            wait()
            assert workshop.list.rowCount() == 1, workshop.status.text()
            workshop.list.selectRow(0)
            with patch(
                "modmesh.apps.desktop.workshop.QDesktopServices.openUrl", return_value=True
            ) as opened:
                workshop.open_selected()
                assert opened.call_args[0][0].toString().endswith("id=123")
            workshop.set_context(None)
            assert workshop.list.rowCount() == 0
            workshop.close()
            rule_game = root / "User Game"
            rule_game.mkdir()
            (rule_game / "Game.exe").write_bytes(b"game fixture; never executed")
            registrations = []
            rules = RulesPanel(
                settings, DiscoveryContext(Platform.WINDOWS, home=root), start, registrations.append
            )
            rules.game_id.setText("gui-game")
            rules.game_name.setText("사용자 게임")
            rules.executable.setText("Game.exe")
            rules.folder.setText(str(rule_game))
            rules.generate()
            rules.check()
            wait()
            assert rules.approve.isEnabled(), rules.status.text()
            assert store.get("rules.installed", "gui-game") is None
            rules.editor.setPlainText(rules.editor.toPlainText() + "\n")
            assert not rules.approve.isEnabled()
            rules.check()
            wait()
            rules.register()
            wait()
            assert len(registrations) == 1, rules.status.text()
            assert store.get("rules.installed", "gui-game") is not None
            registry = create_game_registry(settings)
            installation = registry.get("gui-game").inspect(rule_game, Platform.WINDOWS)
            assert installation is not None
            custom_rule = CustomPanel(settings, start, games=registry)
            custom_rule.set_context(installation)
            wait()
            assert custom_rule.register_button.isEnabled()
            custom_rule.name.setText("Custom game mod")
            custom_rule.location.setText(str(local))
            custom_rule.register()
            wait()
            assert custom_rule.list.rowCount() == 1, custom_rule.status.text()
            search_rule = SearchPanel(settings, store, start)
            search_rule.set_context(installation)
            assert search_rule.game.currentData() == "gui-game"
            search_rule.close()
            custom_rule.close()
            rules.close()
            panel.server.setText("http://127.0.0.1")
            panel.select_server()
            wait()
            assert panel.account is None and store.get("desktop", "server")["url"]
            panel.close()
        database.close()
    print("Account/publishing UI, server isolation, token vault and revocation safety passed")


if __name__ == "__main__":
    verify()
