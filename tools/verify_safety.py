import asyncio
import hashlib
import logging
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from modmesh.adapters.games.palworld import PalworldAdapter
from modmesh.core.cache import ArtifactCache
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadResult, DownloadState
from modmesh.core.games import GameRegistry
from modmesh.core.hashing import verify_file
from modmesh.core.planning import InstallationPlanner, InstallInput
from modmesh.core.resolver import DependencyResolution, ResolvedMod
from modmesh.core.sources import DownloadAccess, DownloadMode
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import ModFile, ModVersion, Platform, SourceProject, SourceRef

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


async def check_cache(root: Path) -> None:
    fetches = 0

    async def fetch(path: Path) -> DownloadResult:
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.01)
        path.write_bytes(DATA)
        return DownloadResult(
            path, verify_file(path, FILE.hashes), "example", FILE.file_id, "example", "job"
        )

    cache_dir = root / "cache"
    with ArtifactCache(cache_dir, max_bytes=len(DATA) * 2) as cache:
        first, second = await asyncio.gather(cache.obtain(FILE, fetch), cache.obtain(FILE, fetch))
        assert fetches == 1 and first.path == second.path
        assert {first.cache_hit, second.cache_hit} == {True, False}
        exported = await cache.export_async(first, root / "export.bin")
        exported.write_bytes(b"user edited export")
        assert first.path.read_bytes() == DATA
        try:
            await cache.export_async(first, exported)
        except ModMeshError as exc:
            assert exc.code == ErrorCode.CONFLICT
        else:
            raise AssertionError("Cache export overwrote a user file")

        alias = replace(FILE, project=SourceRef("other", "project"), file_id="other-file")
        assert (await cache.obtain(alias, fetch)).cache_hit and fetches == 1
        changed = replace(FILE, hashes=(FileHash(HashAlgorithm.SHA256, "0" * 64),))
        assert cache.lookup(changed) is None and first.path.read_bytes() == DATA
        first.path.write_bytes(b"corrupt")
        assert cache.lookup(FILE) is None and not first.path.exists()
        repaired = await cache.obtain(FILE, fetch)
        assert not repaired.cache_hit and fetches == 2

        replacement = root / "replacement"
        replacement.write_bytes(b"user replacement")
        repaired.path.unlink()
        replacement.rename(repaired.path)
        assert cache.lookup(FILE) is None and repaired.path.read_bytes() == b"user replacement"
        unknown = cache.root / "objects" / ("f" * 64)
        unknown.write_bytes(b"unindexed user file")
        assert cache.prune(clear=True).bytes == 0
        assert (
            unknown.read_bytes() == b"unindexed user file"
            and repaired.path.read_bytes() == b"user replacement"
        )
        assert exported.read_bytes() == b"user edited export"
    unowned = root / "unowned" / "modmesh-cache-v1"
    unowned.mkdir(parents=True)
    (unowned / "keep").write_bytes(b"user data")
    try:
        ArtifactCache(unowned.parent)
    except ModMeshError as exc:
        assert exc.code == ErrorCode.CONFLICT
    else:
        raise AssertionError("Nonempty namespace was adopted")
    assert (unowned / "keep").read_bytes() == b"user data"

    with ArtifactCache(root / "quota", max_bytes=len(DATA)) as cache:
        old = await cache.obtain(FILE, fetch)
        content = b"new artifact"
        new_file = replace(
            FILE,
            file_id="file-2",
            size=len(content),
            hashes=(FileHash(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()),),
        )

        async def fetch_new(path: Path) -> DownloadResult:
            path.write_bytes(content)
            return DownloadResult(
                path, verify_file(path, new_file.hashes), "example", "file-2", "example", "job"
            )

        new = await cache.obtain(new_file, fetch_new)
        assert not old.path.exists() and new.path.read_bytes() == content
        assert cache.info().bytes <= len(DATA)
        new.path.write_bytes(b"invalid")
        assert cache.verify() == {"checked": 1, "corrupt_removed": 1}
        assert cache.info().bytes == 0


async def cache_worker(root: Path) -> None:
    with ArtifactCache(root) as cache:

        async def fetch(path: Path) -> DownloadResult:
            with (root / "fetch-once").open("xb") as count:
                count.write(b"1")
            await asyncio.sleep(0.2)
            path.write_bytes(DATA)
            return DownloadResult(
                path, verify_file(path, FILE.hashes), "example", FILE.file_id, "example", "job"
            )

        entry = await cache.obtain(FILE, fetch)
        assert entry.path.read_bytes() == DATA
        print(entry.cache_hit)


async def check_processes(root: Path) -> None:
    with ArtifactCache(root):
        pass
    children = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--cache-worker", str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        results = [await asyncio.to_thread(child.communicate, timeout=20) for child in children]
        assert all(child.returncode == 0 for child in children), results
        assert sorted(output.strip() for output, error in results) == ["False", "True"], results
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


def check_planning(root: Path) -> None:
    game = root / "game"
    binary = game / "Pal/Binaries/Win64/Palworld-Win64-Shipping.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ")
    (game / "Palworld.exe").write_bytes(b"MZ")
    adapter = PalworldAdapter()
    installation = adapter.inspect(game, Platform.WINDOWS)
    assert installation is not None
    registry = GameRegistry()
    registry.register(adapter)
    planner = InstallationPlanner(registry)
    archive = root / "package.zip"

    def plan():
        file = ModFile(
            SourceRef("example", "p"),
            "zip",
            "package.zip",
            archive.stat().st_size,
            hashes=(
                FileHash(HashAlgorithm.SHA256, hashlib.sha256(archive.read_bytes()).hexdigest()),
            ),
        )
        project = SourceProject(file.project, "palworld", "package")
        version = ModVersion(file.project, "1", "1", files=(file,), dependencies_known=True)
        mod = ResolvedMod(project, version, file, True)
        resolution = DependencyResolution("palworld", None, None, (mod,), ())
        return planner.build(
            installation, resolution, (InstallInput(mod, archive, "palworld-pak"),)
        )

    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("one.pak", b"payload")
    first = plan()
    assert first.applicable and first.fingerprint == plan().fingerprint
    assert not (game / "Pal/Content/Paks").exists()
    destination = game / "Pal/Content/Paks/~mods/one.pak"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"user content")
    assert not plan().applicable and destination.read_bytes() == b"user content"
    for name in ("../escape.pak", "/escape.pak", r"C:\escape.pak", "CON.pak", "one.pak:stream"):
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(name, b"bad")
        try:
            plan()
        except ModMeshError:
            pass
        else:
            raise AssertionError("Unsafe archive path was accepted")
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("A.pak", b"one")
        package.writestr("a.pak", b"two")
    try:
        plan()
    except ModMeshError as exc:
        assert exc.code == ErrorCode.CONFLICT
    else:
        raise AssertionError("Case-colliding archive was accepted")
    with zipfile.ZipFile(archive, "w") as package:
        info = zipfile.ZipInfo("link.pak")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(info, "outside")
    try:
        plan()
    except ModMeshError:
        pass
    else:
        raise AssertionError("Archive link was accepted")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("large.pak", b"0" * 100000)
    try:
        plan()
    except ModMeshError:
        pass
    else:
        raise AssertionError("Excessive compression ratio was accepted")
    assert destination.read_bytes() == b"user content"


async def main() -> None:
    logging.disable(logging.CRITICAL)
    with TemporaryDirectory(prefix="modmesh-safety-") as name:
        await asyncio.wait_for(check_downloads(Path(name)), timeout=20)
        await asyncio.wait_for(check_cache(Path(name)), timeout=20)
        await check_processes(Path(name) / "processes")
        check_planning(Path(name))
    print("Download, cache and installation-plan safety checks passed")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--cache-worker":
        asyncio.run(cache_worker(Path(sys.argv[2])))
    else:
        asyncio.run(main())
