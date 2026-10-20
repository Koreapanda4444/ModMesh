from __future__ import annotations

import importlib
import json
import re
import sys
import threading
import time
from typing import Protocol, cast

import httpx

from modmesh import __version__
from modmesh.core.config import validate_server_url
from modmesh.core.diagnostics import ErrorCode, ModMeshError, register_secret
from modmesh.storage.installed import object_record


class TokenVault(Protocol):
    def get(self, server: str) -> str | None: ...
    def set(self, server: str, value: str | None) -> None: ...


class NativeStore(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, value: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class SystemVault:
    def backend(self) -> NativeStore:
        modules = {
            "win32": ("Windows", "WinVaultKeyring"),
            "darwin": ("macOS", "Keyring"),
            "linux": ("SecretService", "Keyring"),
        }
        module, name = modules.get(sys.platform, ("fail", "Keyring"))
        return cast(
            NativeStore, getattr(importlib.import_module("keyring.backends." + module), name)()
        )

    def get(self, server: str) -> str | None:
        try:
            value: object = self.backend().get_password("ModMesh", server)
            return value if isinstance(value, str) else None
        except Exception as exc:
            raise ModMeshError(ErrorCode.STORAGE, "OS credential store is unavailable") from exc

    def set(self, server: str, value: str | None) -> None:
        try:
            backend = self.backend()
            if value is None:
                if backend.get_password("ModMesh", server) is not None:
                    backend.delete_password("ModMesh", server)
            else:
                backend.set_password("ModMesh", server, value)
        except Exception as exc:
            raise ModMeshError(ErrorCode.STORAGE, "OS credential store is unavailable") from exc


class AccountClient:
    def __init__(
        self,
        server: str,
        vault: TokenVault | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server = validate_server_url(server) or ""
        if not self.server:
            raise ModMeshError(ErrorCode.CONFIGURATION, "Select a server URL first")
        self.vault = vault or SystemVault()
        self.transport = transport
        self.lock = threading.RLock()
        self._token: str | None = None
        self._expires = 0
        self._remembered = False

    def clear(self) -> None:
        with self.lock:
            self._token, self._expires = None, 0
            if self._remembered:
                self.vault.set(self.server, None)
                self._remembered = False

    def request(
        self, method: str, path: str, body: object = None, *, authenticated: bool = True
    ) -> dict[str, object]:
        if not path.startswith("/v1/") or "?" in path or "#" in path or ".." in path:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid server API path")
        with self.lock:
            headers = {"User-Agent": f"ModMesh/{__version__}", "Accept": "application/json"}
            if authenticated:
                if not self._token or self._expires <= int(time.time()):
                    self.clear()
                    raise ModMeshError(ErrorCode.AUTHENTICATION, "Sign in to the selected server")
                headers["Authorization"] = "Bearer " + self._token
            try:
                with (
                    httpx.Client(
                        timeout=20, follow_redirects=False, transport=self.transport
                    ) as client,
                    client.stream(
                        method, self.server + path, json=body, headers=headers
                    ) as response,
                ):
                    if response.status_code == 401 and authenticated:
                        self.clear()
                    if not 200 <= response.status_code < 300:
                        code = {
                            401: ErrorCode.AUTHENTICATION,
                            404: ErrorCode.NOT_FOUND,
                            409: ErrorCode.CONFLICT,
                            422: ErrorCode.VALIDATION,
                            429: ErrorCode.RATE_LIMIT,
                        }.get(response.status_code, ErrorCode.NETWORK)
                        raise ModMeshError(
                            code, f"Server request failed (HTTP {response.status_code})"
                        )
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 3 * 1024**2:
                            raise ModMeshError(ErrorCode.NETWORK, "Server response is too large")
                    return object_record(json.loads(payload))
            except (httpx.HTTPError, ValueError) as exc:
                raise ModMeshError(
                    ErrorCode.NETWORK, "Cannot read the selected server response"
                ) from exc

    def login(self, email: str, password: str, remember: bool = False) -> dict[str, object]:
        register_secret(password)
        with self.lock:
            if self._token:
                self.logout()
            result = self.request(
                "POST",
                "/v1/auth/login",
                {"email": email, "password": password},
                authenticated=False,
            )
            token, expires = result.get("access_token"), result.get("expires_at")
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
                or type(expires) is not int
                or expires <= int(time.time())
            ):
                raise ModMeshError(ErrorCode.NETWORK, "Invalid server session response")
            register_secret(token)
            self._token, self._expires = token, expires
            account = object_record(result.get("account"))
            if remember:
                try:
                    self.vault.set(self.server, json.dumps({"token": token, "expires": expires}))
                    self._remembered = True
                except ModMeshError:
                    account["storage_notice"] = (
                        "보안 저장소를 사용할 수 없어 이번 실행에만 로그인합니다."
                    )
            return account

    def restore(self) -> dict[str, object] | None:
        with self.lock:
            value = self.vault.get(self.server)
            if value is None:
                return None
            self._remembered = True
            try:
                data = object_record(json.loads(value))
                token, expires = data.get("token"), data.get("expires")
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                    raise ValueError("Invalid stored token")
                if type(expires) is not int or expires <= int(time.time()):
                    raise ValueError("Expired stored token")
                register_secret(token)
                self._token, self._expires = token, expires
            except (ValueError, ModMeshError):
                self.clear()
                return None
            return self.request("GET", "/v1/account")

    def logout(self, all_sessions: bool = False) -> None:
        with self.lock:
            try:
                if self._token:
                    self.request(
                        "POST", "/v1/auth/logout-all" if all_sessions else "/v1/auth/logout"
                    )
            finally:
                self.clear()

    def auth_action(self, action: str, body: dict[str, object]) -> dict[str, object]:
        if action not in {
            "register",
            "verification",
            "verify",
            "recovery",
            "reset",
            "password",
            "email",
            "email/confirm",
        }:
            raise ModMeshError(ErrorCode.VALIDATION, "Unknown account action")
        for key in ("password", "current_password", "code"):
            if isinstance(value := body.get(key), str):
                register_secret(value)
        result = self.request(
            "POST", "/v1/auth/" + action, body, authenticated=action in {"password", "email"}
        )
        if action in {"password", "reset", "email/confirm"}:
            self.clear()
        return result
