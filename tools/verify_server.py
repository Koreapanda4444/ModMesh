from __future__ import annotations

import asyncio
import os
import socketserver
import threading
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from sqlalchemy import inspect, select
from sqlalchemy.engine import URL

from modmesh.apps.server.auth import Authentication, digest
from modmesh.apps.server.database import Database
from modmesh.apps.server.mail import SMTPMailer, normalize_email
from modmesh.apps.server.main import MAX_REQUEST_BYTES, create_app
from modmesh.apps.server.schema import auth_tokens, sessions, users
from modmesh.core.config import ServerSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.packs import PackContext, PackManifest
from modmesh.domain.models import Loader, Platform


class MailSink:
    ready = True

    def __init__(self):
        self.messages = []

    def send(self, kind, recipient, token):
        self.messages.append((kind, recipient, token))

    def last(self, kind, recipient):
        return next(
            token
            for event, target, token in reversed(self.messages)
            if event == kind and target == recipient
        )


def verify_smtp(settings):
    assert normalize_email("person@bücher.de") == "person@xn--bcher-kva.de"
    messages = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 local verification SMTP\r\n")
            while line := self.rfile.readline():
                command = line.split(b" ", 1)[0].strip().upper()
                if command in {b"EHLO", b"HELO"}:
                    self.wfile.write(b"250-local\r\n250 SIZE 1000000\r\n")
                elif command == b"DATA":
                    self.wfile.write(b"354 End with dot\r\n")
                    payload = b""
                    while (item := self.rfile.readline()) != b".\r\n":
                        if not item:
                            return
                        payload += item
                    messages.append(BytesParser(policy=policy.default).parsebytes(payload))
                    self.wfile.write(b"250 Stored\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 Bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 OK\r\n")

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            mailer = SMTPMailer(
                replace(
                    settings,
                    smtp_host="127.0.0.1",
                    smtp_port=server.server_address[1],
                    smtp_from="sender@example.com",
                    smtp_tls=False,
                )
            )
            mailer.send("verify", "recipient@example.com", "A" * 43)
            assert len(messages) == 1 and "A" * 43 in messages[0].get_content()
        finally:
            server.shutdown()
            thread.join(timeout=5)


async def verify_authentication(client, app, database, sink):
    identity = uuid4().hex
    first, second = f"a-{identity}@example.com", f"b-{identity}@example.com"
    password = "Integration password phrase 123!"
    changed = "Updated integration password 456!"
    final = "Final integration password phrase 789!"
    service = app.state.authentication
    assert isinstance(service, Authentication)

    async def post(path, body=None, headers=None, expected=200):
        response = await client.post("/v1/" + path, json=body, headers=headers)
        assert response.status_code == expected, (path, response.status_code)
        assert response.headers.get("cache-control") == "no-store"
        return response.json()

    async def login(email, password):
        result = await post("auth/login", {"email": email, "password": password})
        return {"authorization": "Bearer " + result["access_token"]}, result["account"]

    assert (await client.get("/v1/account")).status_code == 401
    bad = await client.post("/v1/auth/register", json={"email": first, "password": "short-secret"})
    assert bad.status_code == 422 and "short-secret" not in bad.text
    mass = await client.post(
        "/v1/auth/register", json={"email": first, "password": password, "is_admin": True}
    )
    assert mass.status_code == 422 and password not in mass.text

    async def chunks():
        yield b" " * MAX_REQUEST_BYTES
        yield b"x"

    oversized = await client.post(
        "/v1/auth/login", content=chunks(), headers={"content-type": "application/json"}
    )
    assert oversized.status_code == 413

    registered = await post("auth/register", {"email": first, "password": password}, expected=202)
    await post("auth/login", {"email": first, "password": password}, expected=401)
    verification = sink.last("verify", first)
    await post("auth/verify", {"code": verification})
    await post("auth/verify", {"code": verification}, expected=401)
    duplicate = await post("auth/register", {"email": first, "password": changed}, expected=202)
    assert registered == duplicate
    await post("auth/register", {"email": second, "password": password}, expected=202)
    await post("auth/verify", {"code": sink.last("verify", second)})
    owner, owner_info = await login(first, password)
    other, other_info = await login(second, password)
    retained, _ = await login(first, password)
    assert (await client.get("/v1/accounts/" + owner_info["id"], headers=other)).status_code == 404
    assert (await client.get("/v1/account", headers=owner)).json()["id"] == owner_info["id"]
    await post("auth/logout", headers=owner)
    assert (await client.get("/v1/account", headers=owner)).status_code == 401
    assert (await client.get("/v1/account", headers=retained)).status_code == 200
    recovery = await post("auth/recovery", {"email": first}, expected=202)
    unknown = await post("auth/recovery", {"email": f"absent-{identity}@example.com"}, expected=202)
    assert recovery == unknown
    code = sink.last("reset", first)
    await post("auth/verify", {"code": code}, expected=401)
    raced = await asyncio.gather(
        *(client.post("/v1/auth/reset", json={"code": code, "password": changed}) for _ in range(2))
    )
    assert sorted(response.status_code for response in raced) == [200, 401]
    await post("auth/reset", {"code": code, "password": changed}, expected=401)
    assert (await client.get("/v1/account", headers=retained)).status_code == 401
    await post("auth/login", {"email": first, "password": password}, expected=401)
    current, _ = await login(first, changed)
    await post(
        "auth/password",
        {"current_password": password, "password": final},
        headers=current,
        expected=401,
    )
    await post("auth/password", {"current_password": changed, "password": final}, headers=current)
    assert (await client.get("/v1/account", headers=current)).status_code == 401
    current, _ = await login(first, final)
    new_email = f"new-{identity}@example.com"
    await post("auth/email", {"email": new_email, "password": final}, headers=current, expected=202)
    await post("auth/email/confirm", {"code": sink.last("email", new_email)})
    assert (await client.get("/v1/account", headers=current)).status_code == 401
    owner, owner_info = await login(new_email, final)
    with database.transaction() as connection:
        connection.execute(
            sessions.update()
            .where(sessions.c.token_hash == digest(owner["authorization"].split()[1]))
            .values(expires_at=int(service.clock()) - 1)
        )
    assert (await client.get("/v1/account", headers=owner)).status_code == 401
    owner, owner_info = await login(new_email, final)
    extra, _ = await login(second, password)
    await post("auth/logout-all", headers=extra)
    assert (await client.get("/v1/account", headers=other)).status_code == 401
    other, other_info = await login(second, password)
    expired = f"expired-{identity}@example.com"
    await post("auth/register", {"email": expired, "password": password}, expected=202)
    old_code = sink.last("verify", expired)
    now = int(service.clock())
    service.clock = lambda: now + 86401
    await post("auth/verify", {"code": old_code}, expected=401)
    rate_email = f"limit-{identity}@example.com"
    for _ in range(10):
        service.limit("login-email", rate_email, 10)
    await post("auth/login", {"email": rate_email, "password": password}, expected=429)
    restarted = Authentication(database, app.state.settings, sink, clock=service.clock)
    try:
        restarted.limit("login-email", rate_email, 10)
    except ModMeshError as exc:
        assert exc.code == ErrorCode.RATE_LIMIT
    else:
        raise AssertionError("Rate limit was lost after service restart")
    with database.engine.connect() as connection:
        hashes = connection.execute(select(users.c.password_hash)).scalars().all()
        assert hashes and all(value.startswith("$argon2id$") for value in hashes)
        for table in (sessions, auth_tokens):
            values = connection.execute(select(table.c.token_hash)).scalars().all()
            assert all(
                len(value) == 64 and value not in {verification, code, old_code} for value in values
            )
    print("Account codes, recovery, sessions, ownership, persistent limits and SMTP passed")
    return {"owner": owner, "owner_info": owner_info, "other": other, "other_info": other_info}


async def verify_publishing(client, identities):
    owner, other = identities["owner"], identities["other"]
    context = PackContext(
        "minecraft", "java", Platform.WINDOWS, "1.21.1", None, Loader("fabric", "0.16.14")
    )
    manifest = PackManifest.create("Published pack", context, ())
    pack_id = manifest.pack_id
    path = "/v1/packs/" + pack_id

    async def call(method, endpoint, body=None, headers=owner, expected=200):
        response = await client.request(method, endpoint, json=body, headers=headers)
        assert response.status_code == expected, (endpoint, response.status_code, response.text)
        assert response.headers.get("cache-control") == "no-store"
        return response.json()

    body = {"manifest_json": manifest.dumps().decode(), "visibility": "private"}
    await call("POST", "/v1/packs", body, headers={}, expected=401)
    await call("POST", "/v1/packs", {**body, "owner_id": "forged"}, expected=422)
    await call("POST", "/v1/packs", {**body, "manifest_json": '{"secret":"x"}'}, expected=422)
    await call("POST", "/v1/packs", {**body, "manifest_json": '{"a":1,"a":2}'}, expected=422)
    created = await call("POST", "/v1/packs", body, expected=201)
    assert created["owner_id"] == identities["owner_info"]["id"]
    await call("POST", "/v1/packs", body, expected=409)
    assert pack_id in [item["id"] for item in (await call("GET", "/v1/account/packs"))["packs"]]
    assert pack_id not in [
        item["id"] for item in (await call("GET", "/v1/account/packs", headers=other))["packs"]
    ]
    for endpoint in (path, path + "/versions", path + "/versions/1"):
        await call("GET", endpoint, headers={}, expected=404)
        await call("GET", endpoint, headers=other, expected=404)
    original = await call("GET", path + "/versions/1")
    assert original["digest"] == manifest.digest
    edit = {"visibility": "public", "description": "Shared description", "archived": False}
    await call("PATCH", path, edit, headers=other, expected=404)
    await call("PATCH", path, edit)
    assert (await call("GET", path, headers={}))["visibility"] == "public"
    assert (await call("GET", path + "/versions/1", headers={}))["digest"] == manifest.digest
    newer = replace(manifest, name="Published v2", revision=2)
    version_body = {"manifest_json": newer.dumps().decode(), "expected_revision": 1}
    await call("POST", path + "/versions", version_body, headers=other, expected=404)
    raced = await asyncio.gather(
        *(client.post(path + "/versions", json=version_body, headers=owner) for _ in range(2))
    )
    assert sorted(response.status_code for response in raced) == [201, 409]
    assert (await call("GET", path + "/versions/1"))["digest"] == original["digest"]
    await call("POST", path + "/versions", version_body, expected=409)
    await call(
        "PATCH", path + "/versions/1", {"manifest_json": newer.dumps().decode()}, expected=405
    )
    history = await call("GET", path + "/versions?limit=1")
    assert len(history["versions"]) == 1 and history["next_offset"] == 1
    second = await call("GET", path + "/versions?limit=1&offset=1")
    assert second["versions"][0]["revision"] == 1
    await call("PATCH", path, {**edit, "visibility": "unlisted"})
    await call("GET", path, headers={}, expected=404)
    await call("PATCH", path, {**edit, "archived": True})
    await call("GET", path + "/versions/1", headers={}, expected=404)
    await call(
        "POST",
        path + "/versions",
        {"manifest_json": replace(newer, revision=3).dumps().decode(), "expected_revision": 2},
        expected=409,
    )
    await call("PATCH", path, edit)
    assert (await call("GET", path + "/versions/2", headers={}))["digest"] == newer.digest
    print(
        "Publishing ownership, private access, immutable history, races and archive checks passed"
    )
    return {"manifest": manifest, "newer": newer, "owner": owner, "other": other}


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
        sink = MailSink()
        app = create_app(settings, database, sink)
        verify_smtp(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            health = await client.get("/health")
            assert health.status_code == 200 and health.json()["database"] == database.backend
            info = await client.get("/v1/info")
            assert info.status_code == 200 and info.json()["schema"] == revision
            response = await client.post(
                "/unknown", content=b"x", headers={"content-length": str(MAX_REQUEST_BYTES + 1)}
            )
            assert response.status_code == 413
            identities = await verify_authentication(client, app, database, sink)
            await verify_publishing(client, identities)
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
