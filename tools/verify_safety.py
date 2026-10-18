"""Offline regressions for file loss/corruption risks; temporary directories only."""

import asyncio
import hashlib
import logging
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadState
from modmesh.core.sources import DownloadAccess, DownloadMode
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import ModFile, SourceRef

DATA = b"verified bytes\x00" * 40000
FILE = ModFile(
    SourceRef("example", "project"),
    "file-1",
    "mod.bin",
    len(DATA),
    hashes=(FileHash(HashAlgorithm.SHA256, hashlib.sha256(DATA).hexdigest()),),
)
ACCESS = DownloadAccess(DownloadMode.DIRECT, "https://cdn.example.org/mod.bin")


async def public_addresses(host: str, port: int) -> tuple[str, ...]:
    return ("1.1.1.1",)


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield DATA[:300000]
        raise httpx.ReadError("Connection interrupted")


class WaitingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield DATA[:300000]
        await asyncio.Event().wait()


async def check_downloads(root: Path) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        calls += 1
        return (
            httpx.Response(200, stream=BrokenStream())
            if calls == 1
            else httpx.Response(200, content=DATA)
        )

    async with DownloadManager(
        {"example": ("example.org",)},
        resolver=public_addresses,
        transport=httpx.MockTransport(respond),
        policy=DownloadPolicy(backoff=0),
    ) as manager:
        output = root / "success.bin"
        result = await manager.download(FILE, ACCESS, output)
        assert calls == 2 and output.read_bytes() == DATA
        assert result.artifact.source_verification == "strong"
        try:
            await manager.download(FILE, ACCESS, output)
        except ModMeshError as exc:
            assert exc.code == ErrorCode.CONFLICT
        else:
            raise AssertionError("Existing file was overwritten")
        assert calls == 2 and output.read_bytes() == DATA
        bad = replace(FILE, hashes=(FileHash(HashAlgorithm.SHA256, "0" * 64),))
        try:
            await manager.download(bad, ACCESS, root / "bad.bin")
        except ModMeshError as exc:
            assert exc.code == ErrorCode.INTEGRITY
        else:
            raise AssertionError("Corrupt bytes were published")
        assert not (root / "bad.bin").exists()
        # A late-created destination must also survive publication races.
        raced = root / "raced.bin"

        def occupy(event) -> None:
            if event.state == DownloadState.VERIFYING:
                raced.write_bytes(b"user data")

        try:
            await manager.download(FILE, ACCESS, raced, progress=occupy)
        except ModMeshError as exc:
            assert exc.code == ErrorCode.CONFLICT
        else:
            raise AssertionError("Late-created file was overwritten")
        assert raced.read_bytes() == b"user data"

    async with DownloadManager(
        {"example": ("example.org",)},
        resolver=public_addresses,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=WaitingStream())),
        policy=DownloadPolicy(concurrency=1),
    ) as manager:
        active = manager.submit(FILE, ACCESS, root / "cancelled.bin")
        queued = manager.submit(FILE, ACCESS, root / "queued.bin")
        while active.progress.downloaded == 0:
            await asyncio.sleep(0.001)
        active.cancel()
        queued.cancel()
        results = await asyncio.gather(active.wait(), queued.wait(), return_exceptions=True)
        assert all(isinstance(value, asyncio.CancelledError) for value in results)
        assert all(value.state == DownloadState.CANCELLED for value in manager.jobs)
    assert not (root / "cancelled.bin").exists() and not (root / "queued.bin").exists()
    assert not list(root.glob(".modmesh-*.part"))

    blocked_calls = 0

    def blocked_request(request: httpx.Request) -> httpx.Response:
        nonlocal blocked_calls
        blocked_calls += 1
        return httpx.Response(302, headers={"Location": "https://outside.invalid/file"})

    async def private_addresses(host: str, port: int) -> tuple[str, ...]:
        return ("127.0.0.1",)

    for resolver, access in (
        (private_addresses, ACCESS),
        (public_addresses, DownloadAccess(DownloadMode.DIRECT, "https://outside.invalid/file")),
        (public_addresses, ACCESS),
    ):
        async with DownloadManager(
            {"example": ("example.org",)},
            resolver=resolver,
            transport=httpx.MockTransport(blocked_request),
        ) as manager:
            try:
                await manager.download(FILE, access, root / "blocked.bin")
            except ModMeshError:
                pass
            else:
                raise AssertionError("Unsafe URL was downloaded")
    assert blocked_calls == 1 and not (root / "blocked.bin").exists()
    assert not list(root.glob(".modmesh-*.part"))


async def main() -> None:
    logging.disable(logging.CRITICAL)
    with TemporaryDirectory(prefix="modmesh-safety-") as name:
        await asyncio.wait_for(check_downloads(Path(name)), timeout=20)
    print("Download safety checks passed")


if __name__ == "__main__":
    asyncio.run(main())
