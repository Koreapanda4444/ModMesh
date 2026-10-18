from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import socket
import tempfile
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Self
from uuid import uuid4

import httpx

from modmesh.core.artifact_matching import ArtifactCandidate
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.hashing import verify_file
from modmesh.core.network import USER_AGENT, retry_delay
from modmesh.core.priorities import SourcePriorities, choose_download
from modmesh.core.sources import DownloadAccess, DownloadMode, SourceRegistry
from modmesh.domain.artifacts import Artifact
from modmesh.domain.models import ModFile

type Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
type ProgressCallback = Callable[[DownloadProgress], None]
_logger = logging.getLogger("modmesh.downloads")


class DownloadState(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    RETRYING = "retrying"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class DownloadPolicy:
    concurrency: int = 3
    max_queue: int = 128
    attempts: int = 3
    max_bytes: int = 8 * 1024**3
    timeout: float = 30.0
    max_wait: float = 60.0
    backoff: float = 0.5

    def __post_init__(self) -> None:
        if (
            not 1 <= self.concurrency <= 16
            or not 1 <= self.max_queue <= 1024
            or not 1 <= self.attempts <= 5
            or self.max_bytes < 1
        ):
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid download queue or size limits")
        if (
            not all(math.isfinite(value) for value in (self.timeout, self.max_wait, self.backoff))
            or self.timeout <= 0
            or min(self.max_wait, self.backoff) < 0
        ):
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid download time limits")


@dataclass(frozen=True)
class DownloadProgress:
    job_id: str
    state: DownloadState
    source_id: str
    download_source_id: str
    filename: str
    downloaded: int
    total: int | None
    attempt: int
    error: dict[str, object] | None = None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    artifact: Artifact
    source_id: str
    file_id: str
    download_source_id: str
    job_id: str


@dataclass
class _Job:
    job_id: str
    file: ModFile
    destination: Path
    cancelled: threading.Event
    callback: ProgressCallback | None
    state: DownloadState = DownloadState.QUEUED
    download_source_id: str = ""
    downloaded: int = 0
    total: int | None = None
    attempt: int = 0
    error: dict[str, object] | None = None
    notified_at: float = 0.0

    def snapshot(self) -> DownloadProgress:
        return DownloadProgress(
            self.job_id,
            self.state,
            self.file.project.source_id,
            self.download_source_id or self.file.project.source_id,
            self.file.filename,
            self.downloaded,
            self.total,
            self.attempt,
            self.error,
        )


class DownloadHandle:
    def __init__(self, job: _Job, task: asyncio.Task[DownloadResult]) -> None:
        self._job = job
        self._task = task
        self._loop = asyncio.get_running_loop()

    @property
    def progress(self) -> DownloadProgress:
        return self._job.snapshot()

    def cancel(self) -> None:
        if self._task.done():
            return

        def request() -> None:
            if self._task.done() or self._job.state in {
                DownloadState.COMPLETED,
                DownloadState.FAILED,
                DownloadState.CANCELLED,
                DownloadState.HANDOFF,
            }:
                return
            self._job.cancelled.set()
            if not self._task.cancelling():
                self._task.cancel()

        self._loop.call_soon_threadsafe(request)

    async def wait(self) -> DownloadResult:
        return await self._task


@dataclass(frozen=True)
class _Resolved:
    file: ModFile
    access: DownloadAccess
    source_id: str


class _RetryResponse(Exception):
    def __init__(self, status: int, retry_after: str | None) -> None:
        self.status = status
        self.retry_after = retry_after


async def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    values = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(value[4][0]) for value in values}))


async def _blocking[T](function: Callable[[], T], cancelled: threading.Event) -> T:
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        while not task.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        raise


def publish_file(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise ModMeshError(
            ErrorCode.CONFLICT, "Destination already exists; it was not overwritten"
        ) from exc
    except OSError as exc:
        raise ModMeshError(
            ErrorCode.STORAGE, "Destination does not permit safe file publication"
        ) from exc
    with suppress(OSError):
        temporary.unlink()


class DownloadManager:
    def __init__(
        self,
        allowed_domains: Mapping[str, Sequence[str]],
        *,
        policy: DownloadPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        allow_loopback_http: bool = False,
    ) -> None:
        self.policy = policy or DownloadPolicy()
        self._domains = {
            key: tuple(value.lower().rstrip(".") for value in values)
            for key, values in allowed_domains.items()
        }
        self._resolver = resolver or _resolve_addresses
        self._loopback = allow_loopback_http
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.policy.timeout, connect=min(10, self.policy.timeout)),
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            limits=httpx.Limits(
                max_connections=self.policy.concurrency,
                max_keepalive_connections=self.policy.concurrency,
            ),
        )
        self._semaphore = asyncio.Semaphore(self.policy.concurrency)
        self._handles: dict[str, DownloadHandle] = {}
        self._closed = False

    @property
    def jobs(self) -> tuple[DownloadProgress, ...]:
        return tuple(handle.progress for handle in self._handles.values())

    def _notify(self, job: _Job, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if job.callback is not None and (force or now - job.notified_at >= 0.2):
            job.notified_at = now
            try:
                job.callback(job.snapshot())
            except Exception as exc:
                _logger.warning(
                    "Download progress callback failed",
                    extra={"context": {"error_type": type(exc).__name__}},
                )

    def _submit(
        self,
        file: ModFile,
        destination: Path,
        resolve: Callable[[frozenset[str]], Awaitable[_Resolved]],
        callback: ProgressCallback | None,
    ) -> DownloadHandle:
        if self._closed:
            raise ModMeshError(ErrorCode.CONFLICT, "Download manager is closed")
        active = sum(not handle._task.done() for handle in self._handles.values())
        if active >= self.policy.max_queue:
            raise ModMeshError(ErrorCode.CONFLICT, "Download queue is full")
        for key in list(self._handles):
            if len(self._handles) <= 128:
                break
            if self._handles[key]._task.done():
                del self._handles[key]
        job = _Job(str(uuid4()), file, destination, threading.Event(), callback, total=file.size)
        task = asyncio.create_task(self._run(job, resolve))
        handle = DownloadHandle(job, task)
        self._handles[job.job_id] = handle

        def completed(result: asyncio.Task[DownloadResult]) -> None:
            if result.cancelled():
                if job.state != DownloadState.CANCELLED:
                    job.state = DownloadState.CANCELLED
                    self._notify(job, force=True)
            else:
                result.exception()

        task.add_done_callback(completed)
        self._notify(job, force=True)
        return handle

    def submit(
        self,
        file: ModFile,
        access: DownloadAccess,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> DownloadHandle:
        async def resolve(excluded: frozenset[str]) -> _Resolved:
            if file.project.source_id in excluded:
                raise ModMeshError(ErrorCode.NETWORK, "Source download attempt failed")
            return _Resolved(file, access, file.project.source_id)

        return self._submit(file, destination, resolve, progress)

    def submit_candidate(
        self,
        requested: ArtifactCandidate,
        alternatives: Sequence[ArtifactCandidate],
        registry: SourceRegistry,
        priorities: SourcePriorities,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> DownloadHandle:
        async def resolve(excluded: frozenset[str]) -> _Resolved:
            choice = await choose_download(
                requested, alternatives, registry, priorities, exclude_sources=excluded
            )
            file = replace(
                requested.file,
                hashes=choice.verification_hashes,
                size=requested.file.size
                if requested.file.size is not None
                else choice.download_from.file.size,
            )
            return _Resolved(file, choice.access, choice.download_from.file.project.source_id)

        return self._submit(requested.file, destination, resolve, progress)

    async def download(
        self,
        file: ModFile,
        access: DownloadAccess,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        return await self.submit(file, access, destination, progress=progress).wait()

    async def _url(self, value: str, source_id: str) -> httpx.URL:
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid download URL") from exc
        host = url.host.lower().rstrip(".")
        loopback = (
            self._loopback and host in {"localhost", "127.0.0.1", "::1"} and url.scheme == "http"
        )
        if (
            url.username
            or url.password
            or url.fragment
            or not host
            or (not loopback and (url.scheme != "https" or url.port not in {None, 443}))
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Downloads require a credential-free HTTPS URL"
            )
        if not any(
            host == domain or host.endswith("." + domain)
            for domain in self._domains.get(source_id, ())
        ):
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "Download host is outside the permitted source CDN scope"
            )
        try:
            addresses = await asyncio.wait_for(
                self._resolver(host, url.port or (80 if loopback else 443)),
                timeout=min(10, self.policy.timeout),
            )
        except (OSError, TimeoutError) as exc:
            raise ModMeshError(ErrorCode.NETWORK, "Cannot resolve download host") from exc
        if not addresses:
            raise ModMeshError(ErrorCode.NETWORK, "Download host has no usable addresses")
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise ModMeshError(ErrorCode.NETWORK, "Invalid resolved download address") from exc
            if not (loopback and address.is_loopback) and (
                not address.is_global
                or address.is_multicast
                or address.is_reserved
                or getattr(address, "is_site_local", False)
            ):
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Private or reserved download address blocked"
                )
        return url

    async def _run(
        self, job: _Job, resolve: Callable[[frozenset[str]], Awaitable[_Resolved]]
    ) -> DownloadResult:
        try:
            async with self._semaphore:
                job.destination = job.destination.expanduser()
                job.destination = job.destination.parent.resolve() / job.destination.name
                if job.destination.exists() or job.destination.is_symlink():
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "Destination already exists; it was not overwritten"
                    )
                job.destination.parent.mkdir(parents=True, exist_ok=True)
                excluded: set[str] = set()
                last_error: ModMeshError | None = None
                for _ in range(8):
                    job.state = DownloadState.RESOLVING
                    self._notify(job, force=True)
                    try:
                        resolved = await resolve(frozenset(excluded))
                    except ModMeshError:
                        if last_error is not None:
                            raise last_error from None
                        raise
                    if resolved.access.mode != DownloadMode.DIRECT or resolved.access.url is None:
                        raise ModMeshError(
                            ErrorCode.UNSUPPORTED,
                            resolved.access.reason
                            or "Download requires the official external flow",
                            details={"handoff_url": resolved.access.url},
                        )
                    job.download_source_id = resolved.source_id
                    if resolved.source_id in excluded:
                        raise ModMeshError(
                            ErrorCode.NETWORK, "Download resolver repeated an excluded source"
                        )
                    try:
                        result = await self._transfer(job, resolved)
                    except ModMeshError as exc:
                        if exc.code not in {
                            ErrorCode.NETWORK,
                            ErrorCode.NOT_FOUND,
                            ErrorCode.AUTHENTICATION,
                            ErrorCode.RATE_LIMIT,
                            ErrorCode.INTEGRITY,
                            ErrorCode.UNSUPPORTED,
                        }:
                            raise
                        last_error = exc
                        _logger.warning(
                            "Download source attempt failed",
                            extra={
                                "context": {
                                    "job_id": job.job_id,
                                    "source_id": resolved.source_id,
                                    "error": exc.as_dict(),
                                }
                            },
                        )
                        excluded.add(resolved.source_id)
                        continue
                    job.state = DownloadState.COMPLETED
                    self._notify(job, force=True)
                    return result
                raise last_error or ModMeshError(
                    ErrorCode.NETWORK, "Download source attempt limit reached"
                )
        except asyncio.CancelledError:
            job.cancelled.set()
            job.state = DownloadState.CANCELLED
            self._notify(job, force=True)
            raise
        except (ModMeshError, OSError) as exc:
            error = (
                exc
                if isinstance(exc, ModMeshError)
                else ModMeshError(ErrorCode.STORAGE, "Download filesystem operation failed")
            )
            job.state = (
                DownloadState.HANDOFF
                if error.code == ErrorCode.UNSUPPORTED and "handoff_url" in error.details
                else DownloadState.FAILED
            )
            job.error = error.as_dict()
            self._notify(job, force=True)
            if isinstance(exc, ModMeshError):
                raise
            raise error from exc
        except Exception as exc:
            error = ModMeshError(
                ErrorCode.NETWORK,
                "Unexpected download error",
                details={"error_type": type(exc).__name__},
            )
            job.state = DownloadState.FAILED
            job.error = error.as_dict()
            self._notify(job, force=True)
            raise error from exc

    async def _transfer(self, job: _Job, resolved: _Resolved) -> DownloadResult:
        assert resolved.access.url is not None
        for attempt in range(self.policy.attempts):
            temporary: Path | None = None
            try:
                job.attempt += 1
                job.downloaded = 0
                job.total = resolved.file.size
                if resolved.file.size is not None and resolved.file.size > self.policy.max_bytes:
                    raise ModMeshError(ErrorCode.VALIDATION, "File exceeds the download size limit")
                url = await self._url(resolved.access.url, resolved.source_id)
                descriptor, name = tempfile.mkstemp(
                    prefix=".modmesh-", suffix=".part", dir=job.destination.parent
                )
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as output:
                    for _ in range(5):
                        job.state = DownloadState.DOWNLOADING
                        self._notify(job, force=True)
                        async with self._client.stream("GET", url) as response:
                            status = response.status_code
                            if status in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location:
                                    raise ModMeshError(
                                        ErrorCode.NETWORK, "Download redirect has no destination"
                                    )
                                try:
                                    redirected = str(response.url.join(location))
                                except httpx.InvalidURL as exc:
                                    raise ModMeshError(
                                        ErrorCode.NETWORK, "Invalid CDN redirect URL"
                                    ) from exc
                                url = await self._url(redirected, resolved.source_id)
                                continue
                            if status in {408, 429, 500, 502, 503, 504}:
                                raise _RetryResponse(status, response.headers.get("retry-after"))
                            if status in {401, 403}:
                                raise ModMeshError(
                                    ErrorCode.AUTHENTICATION, "CDN download permission was denied"
                                )
                            if status == 404:
                                raise ModMeshError(ErrorCode.NOT_FOUND, "CDN file is unavailable")
                            if status != 200:
                                raise ModMeshError(
                                    ErrorCode.NETWORK,
                                    "CDN download failed",
                                    details={"status": status},
                                )
                            if response.headers.get("content-encoding", "identity").lower() not in {
                                "",
                                "identity",
                            }:
                                raise ModMeshError(
                                    ErrorCode.NETWORK,
                                    "CDN ignored the uncompressed download request",
                                )
                            raw_length = response.headers.get("content-length")
                            if raw_length is not None:
                                if len(raw_length) > 20 or not raw_length.isdecimal():
                                    raise ModMeshError(
                                        ErrorCode.NETWORK, "Invalid download Content-Length"
                                    )
                                length = int(raw_length)
                                if length > self.policy.max_bytes or (
                                    resolved.file.size is not None and length != resolved.file.size
                                ):
                                    raise ModMeshError(
                                        ErrorCode.INTEGRITY,
                                        "CDN size does not match the requested artifact",
                                    )
                                job.total = length
                            async for chunk in response.aiter_bytes(chunk_size=262144):
                                if job.cancelled.is_set():
                                    raise asyncio.CancelledError
                                if job.downloaded + len(chunk) > self.policy.max_bytes or (
                                    job.total is not None
                                    and job.downloaded + len(chunk) > job.total
                                ):
                                    raise ModMeshError(
                                        ErrorCode.INTEGRITY, "Download exceeded the expected size"
                                    )
                                written = await _blocking(
                                    partial(output.write, chunk), job.cancelled
                                )
                                if written != len(chunk):
                                    raise ModMeshError(
                                        ErrorCode.STORAGE, "Incomplete download file write"
                                    )
                                job.downloaded += len(chunk)
                                self._notify(job)
                            if job.total is not None and job.downloaded != job.total:
                                raise ModMeshError(
                                    ErrorCode.INTEGRITY, "Download ended before its declared size"
                                )
                            break
                    else:
                        raise ModMeshError(ErrorCode.NETWORK, "Download redirect limit reached")

                    def sync_output() -> None:
                        output.flush()
                        os.fsync(output.fileno())

                    await _blocking(sync_output, job.cancelled)
                job.state = DownloadState.VERIFYING
                self._notify(job, force=True)

                def verify(owned_path: Path = temporary) -> Artifact:
                    return verify_file(
                        owned_path,
                        resolved.file.hashes,
                        expected_size=resolved.file.size,
                        cancelled=job.cancelled.is_set,
                    )

                artifact = await _blocking(verify, job.cancelled)
                if job.cancelled.is_set():
                    raise asyncio.CancelledError
                publish_file(temporary, job.destination)
                return DownloadResult(
                    job.destination,
                    artifact,
                    job.file.project.source_id,
                    job.file.file_id,
                    resolved.source_id,
                    job.job_id,
                )
            except (httpx.RequestError, _RetryResponse) as exc:
                if attempt + 1 == self.policy.attempts:
                    raise ModMeshError(
                        ErrorCode.RATE_LIMIT
                        if isinstance(exc, _RetryResponse) and exc.status == 429
                        else ErrorCode.NETWORK,
                        "Download retry limit reached",
                    ) from exc
                delay = retry_delay(
                    exc.retry_after if isinstance(exc, _RetryResponse) else None,
                    fallback=self.policy.backoff * 2**attempt,
                    maximum=86400,
                )
                if delay > self.policy.max_wait:
                    raise ModMeshError(
                        ErrorCode.RATE_LIMIT,
                        "CDN requested a later retry",
                        details={"retry_after": delay},
                    ) from exc
                job.state = DownloadState.RETRYING
                self._notify(job, force=True)
                await asyncio.sleep(delay)
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        _logger.warning("Download temporary-file cleanup failed")
        raise AssertionError("Unreachable download retry state")

    async def aclose(self) -> None:
        self._closed = True
        active = [handle for handle in self._handles.values() if not handle._task.done()]
        for handle in active:
            handle.cancel()
        await asyncio.gather(*(handle.wait() for handle in active), return_exceptions=True)
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
