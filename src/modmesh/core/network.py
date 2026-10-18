from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import unquote

import httpx

from modmesh import __version__
from modmesh.core.diagnostics import ErrorCode, ModMeshError, register_secret

USER_AGENT = f"Koreapanda4444/ModMesh/{__version__} (https://github.com/Koreapanda4444/ModMesh)"


@dataclass(frozen=True)
class HttpPolicy:
    timeout: float = 25.0
    attempts: int = 3
    min_interval: float = 0.25
    backoff: float = 0.5
    response_limit: int = 8 * 1024 * 1024
    max_wait: float = 60.0

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 5 or self.response_limit < 1:
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid HTTP retry/response limits")
        if (
            not all(
                math.isfinite(value)
                for value in (self.timeout, self.min_interval, self.backoff, self.max_wait)
            )
            or self.timeout <= 0
            or min(self.min_interval, self.backoff, self.max_wait) < 0
        ):
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid HTTP timing configuration")


class EnvironmentCredentials:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = environment

    def get(self, source_id: str) -> str | None:
        names = {
            "nexus": ("MODMESH_NEXUS_API_KEY", "NEXUS_API_KEY"),
            "curseforge": ("MODMESH_CURSEFORGE_API_KEY", "CURSEFORGE_API_KEY"),
        }.get(source_id, ())
        value = next(
            (self._environment[name] for name in names if self._environment.get(name)), None
        )
        if value is not None:
            if any(char.isspace() for char in value):
                raise ModMeshError(
                    ErrorCode.AUTHENTICATION, "Source API key contains invalid whitespace"
                )
            register_secret(value)
        return value


def retry_delay(value: str | None, *, fallback: float, maximum: float) -> float:
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                delay = (target - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = fallback
        if math.isfinite(delay):
            return max(0.0, min(delay, maximum))
    return min(fallback, maximum)


class _TransientResponse(Exception):
    def __init__(self, status: int, retry_after: str | None) -> None:
        self.status = status
        self.retry_after = retry_after


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        policy: HttpPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_loopback_http: bool = False,
    ) -> None:
        self.policy = policy or HttpPolicy()
        self.base_url = httpx.URL(base_url.rstrip("/") + "/")
        if (
            self.base_url.username
            or self.base_url.password
            or not self.base_url.host
            or self.base_url.query
            or self.base_url.fragment
        ):
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid source API base URL")
        if self.base_url.scheme != "https" and not (
            allow_loopback_http
            and self.base_url.scheme == "http"
            and self.base_url.host in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ModMeshError(ErrorCode.CONFIGURATION, "Source APIs require HTTPS")
        self._origin = (self.base_url.scheme, self.base_url.host, self.base_url.port)
        self._headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            **(headers or {}),
        }
        for key, value in (headers or {}).items():
            if any(word in key.lower() for word in ("key", "token", "authorization", "cookie")):
                register_secret(value)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.policy.timeout, connect=min(10, self.policy.timeout)),
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    async def _throttle(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._next_request > now:
                delay = self._next_request - now
                if delay > self.policy.max_wait:
                    raise ModMeshError(
                        ErrorCode.RATE_LIMIT,
                        "Source rate limit requires a later retry",
                        details={"retry_after": math.ceil(delay)},
                    )
                await asyncio.sleep(delay)
            self._next_request = asyncio.get_running_loop().time() + self.policy.min_interval

    def _observe_limits(self, headers: httpx.Headers) -> None:
        if headers.get("x-ratelimit-remaining") == "0":
            delay = retry_delay(headers.get("x-ratelimit-reset"), fallback=1.0, maximum=86400)
            self._next_request = max(self._next_request, asyncio.get_running_loop().time() + delay)

    async def _fetch(self, url: httpx.URL, params: Mapping[str, str | int] | None) -> bytes:
        for _ in range(5):
            await self._throttle()
            async with self._client.stream(
                "GET", url, params=params, headers=self._headers
            ) as response:
                self._observe_limits(response.headers)
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ModMeshError(
                            ErrorCode.NETWORK, "Source API redirect has no destination"
                        )
                    target = response.url.join(location)
                    if (
                        (target.scheme, target.host, target.port) != self._origin
                        or target.username
                        or target.password
                    ):
                        raise ModMeshError(
                            ErrorCode.NETWORK, "Cross-origin source API redirect blocked"
                        )
                    url, params = target, None
                    continue
                if status in {408, 429, 500, 502, 503, 504}:
                    raise _TransientResponse(status, response.headers.get("retry-after"))
                if status in {401, 403}:
                    raise ModMeshError(
                        ErrorCode.AUTHENTICATION,
                        "Source API access requires valid credentials or permission",
                        details={"status": status, "host": url.host},
                    )
                if status == 404:
                    raise ModMeshError(ErrorCode.NOT_FOUND, "Source API resource was not found")
                if not 200 <= status < 300:
                    raise ModMeshError(
                        ErrorCode.NETWORK,
                        "Source API request failed",
                        details={"status": status, "host": url.host},
                    )
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise ModMeshError(
                        ErrorCode.NETWORK, "Source API ignored the uncompressed response request"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > self.policy.response_limit:
                        raise ModMeshError(
                            ErrorCode.NETWORK, "Source API response exceeds size limit"
                        )
                    body.extend(chunk)
                return bytes(body)
        raise ModMeshError(ErrorCode.NETWORK, "Source API redirect limit reached")

    async def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> object:
        if (
            path.startswith(("/", "\\"))
            or httpx.URL(path).is_absolute_url
            or any(unquote(part) in {".", ".."} for part in path.split("/"))
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "API paths must remain relative to the configured source"
            )
        target = self.base_url.join(path)
        if (target.scheme, target.host, target.port) != self._origin:
            raise ModMeshError(ErrorCode.VALIDATION, "Source API origin mismatch")
        for attempt in range(self.policy.attempts):
            try:
                body = await self._fetch(target, params)
                try:
                    result: object = json.loads(body, parse_constant=_invalid_json_constant)
                    return result
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ModMeshError(
                        ErrorCode.NETWORK, "Source API returned invalid JSON"
                    ) from exc
            except (httpx.RequestError, _TransientResponse) as exc:
                if attempt + 1 == self.policy.attempts:
                    status = exc.status if isinstance(exc, _TransientResponse) else None
                    raise ModMeshError(
                        ErrorCode.RATE_LIMIT if status == 429 else ErrorCode.NETWORK,
                        "Source API retry limit reached",
                        details={"status": status, "host": self.base_url.host},
                    ) from exc
                delay = retry_delay(
                    exc.retry_after if isinstance(exc, _TransientResponse) else None,
                    fallback=self.policy.backoff * 2**attempt,
                    maximum=86400,
                )
                if delay > self.policy.max_wait:
                    raise ModMeshError(
                        ErrorCode.RATE_LIMIT,
                        "Source requested a longer retry delay",
                        details={"retry_after": delay},
                    ) from exc
                await asyncio.sleep(delay)
        raise AssertionError("Unreachable retry state")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def _invalid_json_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON numeric constant")
