import asyncio
import hashlib
import io
import json
import logging
import os
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

import modmesh.core.installation as installation_engine
import modmesh.core.processes as processes_engine
import modmesh.core.profiles as profile_engine
import modmesh.core.removal as removal_engine
from modmesh.adapters.games.minecraft import MinecraftAdapter
from modmesh.adapters.games.minecraft_mods import matches_native, read_jar, validate_native
from modmesh.adapters.games.palworld import PalworldAdapter
from modmesh.apps.operations import prepare_pack
from modmesh.core.alternatives import find_alternatives
from modmesh.core.artifact_matching import ArtifactCandidate
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import AppPaths, ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadResult, DownloadState
from modmesh.core.games import GameRegistry
from modmesh.core.hashing import verify_file
from modmesh.core.identity import EvidenceKind, IdentityEvidence, IdentityRegistry
from modmesh.core.installation import InstallationManager
from modmesh.core.pack_io import export_profile, read_manifest, resolve_pack, write_manifest
from modmesh.core.packs import PackContext, PackEntry, PackManifest, PackSetting
from modmesh.core.planning import InstallationPlanner, InstallInput
from modmesh.core.priorities import SourcePriorities
from modmesh.core.processes import java_guard
from modmesh.core.profiles import ProfileManager
from modmesh.core.removal import RemovalManager
from modmesh.core.resolver import DependencyEdge, DependencyResolution, ResolvedMod
from modmesh.core.sources import (
    DownloadAccess,
    DownloadMode,
    SourceAdapter,
    SourceCapability,
    SourceDescriptor,
    SourceRegistry,
)
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import Dependency, DependencyKind, DependencyTarget
from modmesh.domain.models import ModFile, ModVersion, Platform, SourceProject, SourceRef
from modmesh.storage.metadata import MetadataStore

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


class LinkedSource(SourceAdapter):
    version_filters = True

    def __init__(self, candidate: ArtifactCandidate, access: DownloadAccess) -> None:
        self.candidate = candidate
        self.access = access
        self.denied = False
        self._descriptor = SourceDescriptor(
            candidate.project.reference.source_id,
            "Safety verification",
            frozenset({"minecraft"}),
            frozenset(SourceCapability),
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def project(self, game_id: str, project_id: str) -> SourceProject:
        return self.candidate.project

    async def versions(self, game_id: str, project_id: str, *, query=None):
        if self.denied:
            raise ModMeshError(ErrorCode.AUTHENTICATION, "Permission unavailable")
        return (self.candidate.version,)

    async def resolve_download(self, game_id: str, file: ModFile) -> DownloadAccess:
        return self.access


async def check_linked_downloads(root: Path) -> None:
    reference = SourceRef("origin", "project")
    file = replace(FILE, project=reference)
    project = SourceProject(reference, "minecraft", "Identical label")
    version = ModVersion(
        reference, "pinned", "1.0", frozenset({"1.21.1"}), frozenset({"fabric"}), files=(file,)
    )
    requested = ArtifactCandidate(project, version, file, "1.21.1", "fabric")
    mirror_ref = SourceRef("mirror", "project")
    mirror_file = replace(file, project=mirror_ref, file_id="mirror-file")
    mirror = ArtifactCandidate(
        replace(project, reference=mirror_ref),
        replace(version, project=mirror_ref, version_id="other-label", files=(mirror_file,)),
        mirror_file,
        "1.21.1",
        "fabric",
    )
    registry = SourceRegistry()
    registry.register(
        LinkedSource(
            requested, DownloadAccess(DownloadMode.EXTERNAL, "https://example.org/project")
        )
    )
    provider = LinkedSource(mirror, ACCESS)
    registry.register(provider)
    with MetadataStore(root / "linked.sqlite3") as store:
        identities = IdentityRegistry(store)
        assert not (await find_alternatives(requested, registry, identities)).candidates
        canonical = identities.get_or_create("minecraft", reference)
        identities.bind(
            canonical,
            "minecraft",
            mirror_ref,
            IdentityEvidence(EvidenceKind.USER_CONFIRMATION, "safety-verification"),
        )
        alternatives = await find_alternatives(requested, registry, identities)
        assert len(alternatives.candidates) == 1
        async with DownloadManager(
            {"mirror": ("example.org",)},
            resolver=public_addresses,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=DATA)),
        ) as manager:
            result = await manager.submit_candidate(
                requested,
                alternatives.candidates,
                registry,
                SourcePriorities({"minecraft": ("origin", "mirror")}),
                root / "linked.bin",
            ).wait()
            assert result.download_source_id == "mirror" and result.source_id == "origin"
            assert result.artifact.sha256 == file.hashes[0] and result.path.read_bytes() == DATA
        weak = replace(
            mirror_file,
            hashes=(
                FileHash(HashAlgorithm.SHA1, hashlib.sha1(DATA, usedforsecurity=False).hexdigest()),
            ),
        )
        provider.candidate = replace(
            mirror, file=weak, version=replace(mirror.version, files=(weak,))
        )
        assert not (await find_alternatives(requested, registry, identities)).candidates
        provider.candidate = replace(
            mirror, version=replace(mirror.version, game_versions=frozenset({"1.20.1"}))
        )
        assert not (await find_alternatives(requested, registry, identities)).candidates
        conflicting = replace(mirror_file, hashes=(FileHash(HashAlgorithm.SHA256, "0" * 64),))
        provider.candidate = replace(
            mirror, file=conflicting, version=replace(mirror.version, files=(conflicting,))
        )
        assert not (await find_alternatives(requested, registry, identities)).candidates
        provider.denied = True
        denied = await find_alternatives(requested, registry, identities)
        assert not denied.candidates and any(
            "Permission unavailable" in note for note in denied.notes
        )
        assert not (
            await find_alternatives(replace(requested, loader="forge"), registry, identities)
        ).candidates
        identities.unbind("minecraft", mirror_ref)
        provider.denied = False
        provider.candidate = mirror
        assert not (await find_alternatives(requested, registry, identities)).candidates
    await registry.aclose()


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


def temporary_installation(root: Path):
    binary = root / "Pal/Binaries/Win64"
    binary.mkdir(parents=True, exist_ok=True)
    (binary / "Palworld-Win64-Shipping.exe").write_bytes(b"MZ")
    (root / "Palworld.exe").write_bytes(b"MZ")
    adapter = PalworldAdapter()
    registry = GameRegistry()
    registry.register(adapter)
    game = adapter.inspect(root, Platform.WINDOWS)
    assert game is not None
    source = root.parent / "install-package.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("One.pak", b"new one")
        archive.writestr("Two.pak", b"new two")
    reference = SourceRef("example", "p")
    file = ModFile(
        reference,
        "install-zip",
        "mod.zip",
        source.stat().st_size,
        hashes=(FileHash(HashAlgorithm.SHA256, hashlib.sha256(source.read_bytes()).hexdigest()),),
    )
    project = SourceProject(reference, "palworld", "package")
    version = ModVersion(reference, "1", "1", files=(file,), dependencies_known=True)
    mod = ResolvedMod(project, version, file, True)
    return (
        InstallationManager(registry, game),
        DependencyResolution("palworld", None, None, (mod,), ()),
        (InstallInput(mod, source, "palworld-pak"),),
    )


def install_crash(root: Path, mode: str) -> None:
    manager, resolution, inputs = temporary_installation(root)
    plan = manager.plan(resolution, inputs, replace_existing=True)
    publish = installation_engine.publish_file

    def interrupt(temporary: Path, destination: Path) -> None:
        if mode == "after":
            publish(temporary, destination)
        os._exit(77)

    installation_engine.publish_file = interrupt
    manager.apply(plan, plan.fingerprint, replace_existing=True)


def removal_crash(root: Path, mode: str) -> None:
    manager, resolution, inputs = temporary_installation(root)
    target = root / "Pal/Content/Paks/~mods/One.pak"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    plan = manager.plan(resolution, inputs, replace_existing=True)
    manager.apply(plan, plan.fingerprint, replace_existing=True)
    remover = RemovalManager(manager.games, manager.installation)
    removal = remover.plan_removal((resolution.mods[0].project.reference,))
    publish = removal_engine.publish_file

    def interrupt(temporary: Path, destination: Path) -> None:
        if mode == "after":
            publish(temporary, destination)
        os._exit(78)

    removal_engine.publish_file = interrupt
    remover.remove(removal, removal.fingerprint)


def check_installation(root: Path) -> None:
    manager, resolution, inputs = temporary_installation(root / "normal")
    target = manager.root / "Pal/Content/Paks/~mods/One.pak"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user original")
    plan = manager.plan(resolution, inputs, replace_existing=True)
    publish = installation_engine.publish_file
    calls = 0

    def fail_once(temporary: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Interrupted publication")
        publish(temporary, destination)

    installation_engine.publish_file = fail_once
    try:
        try:
            manager.apply(plan, plan.fingerprint, replace_existing=True)
        except ModMeshError as exc:
            assert exc.details["recovery"] == "rolled_back"
        else:
            raise AssertionError("Interrupted installation was committed")
    finally:
        installation_engine.publish_file = publish
    assert target.read_bytes() == b"user original" and not manager.installed()
    plan = manager.plan(resolution, inputs, replace_existing=True)
    manager.apply(plan, plan.fingerprint, replace_existing=True)
    assert target.read_bytes() == b"new one" and len(manager.installed()) == 1
    assert manager.installed()[0].mod == resolution.mods[0]
    assert not manager.recover()
    remover = RemovalManager(manager.games, manager.installation)
    removal = remover.plan_removal((resolution.mods[0].project.reference,))
    assert removal.applicable, removal.issues
    target.write_bytes(b"user edit")
    assert not remover.plan_removal((resolution.mods[0].project.reference,)).applicable
    try:
        remover.remove(removal, removal.fingerprint)
    except ModMeshError:
        pass
    else:
        raise AssertionError("Removal accepted a stale plan")
    target.write_bytes(b"new one")
    removal = remover.plan_removal((resolution.mods[0].project.reference,))
    remover.remove(removal, removal.fingerprint)
    assert target.read_bytes() == b"user original"
    assert not (target.parent / "Two.pak").exists() and not remover.installed()
    for mode in ("before", "after"):
        game = root / mode
        manager, resolution, inputs = temporary_installation(game)
        target = game / "Pal/Content/Paks/~mods/One.pak"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"crash original")
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--install-crash", str(game), mode],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert child.returncode == 77, child.stderr
        results = manager.recover()
        assert len(results) == 1 and results[0].state == "rolled_back", results
        assert target.read_bytes() == b"crash original" and not manager.installed()
        assert not (target.parent / "Two.pak").exists() and not manager.recover()

    for mode in ("before", "after"):
        game = root / f"removal-{mode}"
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--removal-crash", str(game), mode],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert child.returncode == 78, child.stderr
        installation = PalworldAdapter().inspect(game, Platform.WINDOWS)
        assert installation is not None
        manager = InstallationManager(manager.games, installation)
        recovered = manager.recover()
        assert len(recovered) == 1 and recovered[0].state == "rolled_back", recovered
        assert (game / "Pal/Content/Paks/~mods/One.pak").read_bytes() == b"new one"
        assert (game / "Pal/Content/Paks/~mods/Two.pak").read_bytes() == b"new two"
        assert len(manager.installed()) == 1 and not manager.recover()


def check_shared_dependencies(root: Path) -> None:
    manager, resolution, inputs = temporary_installation(root)
    base = resolution.mods[0]
    plan = manager.plan(resolution, inputs)
    manager.apply(plan, plan.fingerprint)
    source = root.parent / "dependent.pak"
    source.write_bytes(b"dependent")
    reference = SourceRef("example", "dependent")
    file = ModFile(reference, "d", source.name, source.stat().st_size)
    declaration = Dependency(DependencyKind.REQUIRED, DependencyTarget("example", "p"))
    version = ModVersion(
        reference, "1", "1", files=(file,), dependencies=(declaration,), dependencies_known=True
    )
    mod = ResolvedMod(SourceProject(reference, "palworld", "dependent"), version, file, True)
    combined = replace(
        resolution,
        mods=(base, mod),
        edges=(DependencyEdge(reference, base.project.reference, declaration),),
    )
    plan = manager.plan(combined, (*inputs, InstallInput(mod, source, "palworld-pak")))
    assert plan.applicable, plan.issues
    manager.apply(plan, plan.fingerprint)
    remover = RemovalManager(manager.games, manager.installation)
    blocked = remover.plan_removal((base.project.reference,))
    assert not blocked.applicable and any(issue.code == "required_by" for issue in blocked.issues)
    removal = remover.plan_removal((reference,))
    remover.remove(removal, removal.fingerprint)
    assert len(manager.installed()) == 1 and (root / "Pal/Content/Paks/~mods/One.pak").exists()
    shared_ref = SourceRef("example", "shared")
    shared_file = replace(base.file, project=shared_ref)
    shared_mod = ResolvedMod(
        replace(base.project, reference=shared_ref),
        replace(base.version, project=shared_ref, files=(shared_file,)),
        shared_file,
        True,
    )
    shared_resolution = replace(resolution, mods=(shared_mod,))
    plan = manager.plan(shared_resolution, (replace(inputs[0], mod=shared_mod),))
    assert plan.applicable, plan.issues
    manager.apply(plan, plan.fingerprint)
    removal = remover.plan_removal((base.project.reference,))
    assert all(change.action == "release" for change in removal.changes)
    remover.remove(removal, removal.fingerprint)
    assert (root / "Pal/Content/Paks/~mods/One.pak").exists()
    removal = remover.plan_removal((shared_ref,))
    remover.remove(removal, removal.fingerprint)
    assert not manager.installed() and not (root / "Pal/Content/Paks/~mods/One.pak").exists()


async def check_pack_import(root: Path) -> None:
    manager, resolution, inputs = temporary_installation(root / "source")
    plan = manager.plan(resolution, inputs)
    manager.apply(plan, plan.fingerprint)
    profiles = ProfileManager(manager.games, manager.installation)
    profile = profiles.create("Export")
    manifest = export_profile(profiles, profile.profile_id)
    destination = root / "portable.modmesh.json"
    write_manifest(destination, manifest)
    assert read_manifest(destination).digest == manifest.digest
    try:
        write_manifest(destination, manifest)
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing manifest was overwritten")
    assert str(root) not in destination.read_text()
    target, _, _ = temporary_installation(root / "target")
    settings = ClientSettings(AppPaths(root / "cfg", root / "data", root / "cache"))
    selected = manifest.entries[0].mod
    candidate = ArtifactCandidate(selected.project, selected.version, selected.file, None, None)
    provider = LinkedSource(candidate, ACCESS)
    provider._descriptor = replace(provider.descriptor, games=frozenset({"palworld"}))
    registry = SourceRegistry()
    registry.register(provider)
    with ArtifactCache(settings.paths.cache_dir) as cache:

        async def fetch(path):
            path.write_bytes(inputs[0].path.read_bytes())
            return DownloadResult(
                path,
                verify_file(path, selected.file.hashes),
                "example",
                selected.file.file_id,
                "example",
                "seed",
            )

        await cache.obtain(selected.file, fetch)
    prepared = await prepare_pack(settings, registry, target.installation, manifest)
    try:
        assert prepared.plan.applicable, prepared.plan.issues
        assert prepared.manager.apply(prepared.plan, prepared.plan.fingerprint).state == "committed"
        assert (target.root / "Pal/Content/Paks/~mods/One.pak").read_bytes() == b"new one"
        assert (target.root / "Pal/Content/Paks/~mods/Two.pak").read_bytes() == b"new two"
    finally:
        prepared.close()
    invalid_file = replace(selected.file, hashes=(FileHash(HashAlgorithm.SHA256, "0" * 64),))
    invalid_mod = replace(
        selected, file=invalid_file, version=replace(selected.version, files=(invalid_file,))
    )
    invalid = replace(manifest, entries=(replace(manifest.entries[0], mod=invalid_mod),))
    try:
        await resolve_pack(invalid, registry)
    except ModMeshError as exc:
        assert exc.code == ErrorCode.INTEGRITY
    else:
        raise AssertionError("Changed source file metadata was accepted")
    await registry.aclose()


def check_manifests(root: Path) -> None:
    manager, resolution, inputs = temporary_installation(root)
    entry = PackEntry(resolution.mods[0], "palworld-pak")
    manifest = PackManifest.create(
        "Portable",
        PackContext.from_installation(manager.installation),
        (entry,),
        (PackSetting("options.txt", "0" * 64),),
    )
    payload = manifest.dumps()
    restored = PackManifest.loads(payload)
    assert restored.digest == manifest.digest and restored.entries[0].mod == entry.mod
    assert "download_url" not in payload.decode() and str(root) not in payload.decode()
    assert restored.warnings and restored.settings[0].path == "options.txt"
    for change in (
        {"schema_version": 2},
        {"schema_version": True},
        {"revision": 0},
        {"api_key": "never allowed"},
        {"entries": [entry.document(), entry.document()]},
        {"settings": [{"path": "../secret", "sha256": "0" * 64, "included": False}]},
        {"settings": [{"path": "options.txt", "sha256": "0" * 64, "included": True}]},
    ):
        document = manifest.document() | change
        try:
            PackManifest.loads(json.dumps(document).encode())
        except ModMeshError:
            pass
        else:
            raise AssertionError("Unsafe manifest accepted")
    for payload in (
        b'{"name":"a","name":"b"}',
        b'{"value":NaN}',
        b"[" * 1200,
        b" " * (2 * 1024**2 + 1),
    ):
        try:
            PackManifest.loads(payload)
        except ModMeshError:
            pass
        else:
            raise AssertionError("Unbounded or ambiguous JSON accepted")


def profile_setup(root: Path):
    manager, resolution, inputs = temporary_installation(root)
    target = root / "Pal/Content/Paks/~mods/One.pak"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    plan = manager.plan(resolution, inputs, replace_existing=True)
    manager.apply(plan, plan.fingerprint, replace_existing=True)
    profiles = ProfileManager(manager.games, manager.installation)
    modded = profiles.create("Modded")
    empty = profiles.create("Empty", capture=False)
    return profiles, modded, empty


def profile_crash(root: Path, mode: str) -> None:
    profiles, modded, empty = profile_setup(root)
    plan = profiles.plan_switch(empty.profile_id)
    publish = profile_engine.publish_file

    def interrupt(temporary: Path, destination: Path) -> None:
        if mode == "after":
            publish(temporary, destination)
        os._exit(77)

    profile_engine.publish_file = interrupt
    profiles.switch(plan, plan.fingerprint)


def check_profiles(root: Path) -> None:
    profiles, modded, empty = profile_setup(root)
    target = root / "Pal/Content/Paks/~mods/One.pak"
    second = target.with_name("Two.pak")
    external = target.with_name("external.pak")
    external.write_bytes(b"external")
    save = root / "save.dat"
    save.write_bytes(b"user save")
    for identity, expected in ((empty.profile_id, b"original"), (modded.profile_id, b"new one")):
        plan = profiles.plan_switch(identity)
        assert plan.applicable, plan.issues
        assert profiles.switch(plan, plan.fingerprint).state == "committed"
        assert profiles.active_id() == identity and target.read_bytes() == expected
        assert second.exists() == (identity == modded.profile_id)
        assert external.read_bytes() == b"external" and save.read_bytes() == b"user save"
    assert len(profiles.installed()) == 1
    previous = profiles.active_id()
    plan = profiles.plan_switch(empty.profile_id)
    cancelled = False

    def interrupt(event):
        nonlocal cancelled
        cancelled = True

    try:
        profiles.switch(plan, plan.fingerprint, cancelled=lambda: cancelled, progress=interrupt)
    except ModMeshError as exc:
        assert exc.details["recovery"] == "rolled_back"
    else:
        raise AssertionError("Cancelled profile transition was committed")
    assert profiles.active_id() == previous and target.read_bytes() == b"new one"
    assert second.read_bytes() == b"new two"
    target.write_bytes(b"user change")
    try:
        profiles.plan_switch(empty.profile_id)
    except ModMeshError:
        pass
    else:
        raise AssertionError("Modified managed file was accepted")
    assert target.read_bytes() == b"user change"
    for mode in ("before", "after"):
        crashed = root.parent / ("profile-crash-" + mode)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--profile-crash", str(crashed), mode],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 77, result.stderr
        manager, _, _ = temporary_installation(crashed)
        recovered = manager.recover()
        assert recovered and all(item.state == "rolled_back" for item in recovered)
        assert (crashed / "Pal/Content/Paks/~mods/One.pak").read_bytes() == b"new one"
        assert (crashed / "Pal/Content/Paks/~mods/Two.pak").read_bytes() == b"new two"
        assert manager.installed()


def check_minecraft(root: Path) -> None:
    for loader, uid, version in (
        ("fabric", "net.fabricmc.fabric-loader", "0.16.14"),
        ("forge", "net.minecraftforge", "52.0.1"),
        ("neoforge", "net.neoforged", "21.1.219"),
    ):
        instance = root / loader
        game = instance / "minecraft"
        game.mkdir(parents=True)
        (instance / "instance.cfg").write_text("InstanceType=OneSix")
        (instance / "mmc-pack.json").write_text(
            json.dumps(
                {
                    "formatVersion": 1,
                    "components": [
                        {"uid": "net.minecraft", "version": "1.21.1"},
                        {"uid": uid, "version": version},
                    ],
                }
            )
        )
        source = root / f"{loader}.jar"
        with zipfile.ZipFile(source, "w") as archive:
            if loader == "fabric":
                archive.writestr(
                    "fabric.mod.json",
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "id": "library",
                            "version": "1.0.0",
                            "depends": {"minecraft": "~1.21.1", "fabricloader": ">=0.16.0"},
                        }
                    ),
                )
            else:
                name = "META-INF/mods.toml" if loader == "forge" else "META-INF/neoforge.mods.toml"
                archive.writestr(
                    name,
                    'modLoader="javafml"\nloaderVersion="[4,)"\n[[mods]]\nmodId="library"\nversion="1.0.0"\n',
                )
        adapter = MinecraftAdapter()
        games = GameRegistry()
        games.register(adapter)
        installation = adapter.inspect(game, Platform.WINDOWS)
        assert installation is not None and installation.loader is not None
        manager = RemovalManager(games, installation)
        reference = SourceRef("example", loader)
        file = ModFile(reference, "1", source.name, source.stat().st_size)
        mod = ResolvedMod(
            SourceProject(reference, "minecraft", loader),
            ModVersion(
                reference,
                "1",
                "1",
                frozenset({"1.21.1"}),
                frozenset({loader}),
                files=(file,),
                dependencies_known=True,
            ),
            file,
            True,
        )
        resolution = DependencyResolution("minecraft", "1.21.1", loader, (mod,), ())
        plan = manager.plan(resolution, (InstallInput(mod, source),))
        assert plan.applicable, plan.issues
        manager.apply(plan, plan.fingerprint)
        target = game / "mods" / source.name
        assert target.read_bytes() == source.read_bytes()
        if loader == "fabric":
            options = game / "options.txt"
            options.write_bytes(b"initial settings")
            profiles = ProfileManager(games, installation)
            configured = profiles.create("Configured", settings=("options.txt",))
            empty = profiles.create("Empty", capture=False)
            initial = profiles.active_id()
            for identity in (configured.profile_id, empty.profile_id):
                transition = profiles.plan_switch(identity)
                assert transition.applicable, transition.issues
                profiles.switch(transition, transition.fingerprint)
            assert options.read_bytes() == b"initial settings" and not target.exists()
            options.write_bytes(b"other profile settings")
            transition = profiles.plan_switch(configured.profile_id)
            assert transition.applicable, transition.issues
            profiles.switch(transition, transition.fingerprint)
            assert options.read_bytes() == b"initial settings" and target.exists()
            try:
                profiles.delete(configured.profile_id)
            except ModMeshError:
                pass
            else:
                raise AssertionError("Active profile was deleted")
            transition = profiles.plan_switch(initial)
            profiles.switch(transition, transition.fingerprint)
            assert matches_native("0.8.12-beta.1+mc1.21.1", ("0.8.x",), "fabric")
            assert not matches_native("0.9.0-alpha", ("0.8.x",), "fabric")
            assert matches_native("1.21.1", (">=1.15-alpha.19.39.a",), "fabric")
            assert matches_native("1.2.3.4", (">=1.2.3.3 <1.2.4",), "fabric")
            assert matches_native("0.9.0", ("^0.8.0",), "fabric")
            assert matches_native("1.0.99", ("~1",), "fabric")
            assert not matches_native("1.1.0", ("~1",), "fabric")
            assert not matches_native("1.2-beta", (">=1.2",), "fabric")
            assert matches_native("1.2.0", ("1.2",), "fabric")
            shared = source.read_bytes()
            embedded = []
            for identity in ("first", "second"):
                jar = root / f"{identity}.jar"
                with zipfile.ZipFile(jar, "w") as archive:
                    archive.writestr(
                        "fabric.mod.json",
                        json.dumps(
                            {
                                "schemaVersion": 1,
                                "id": identity,
                                "version": "1.0.0",
                                "depends": {"library": "*"},
                                "jars": [{"file": "nested/library.jar"}],
                            }
                        ),
                    )
                    archive.writestr("nested/library.jar", shared)
                embedded.append((jar.name, read_jar(jar, "fabric")))
            assert validate_native(installation, tuple(embedded)).valid
            duplicate = validate_native(
                installation, (("first.jar", read_jar(source)), ("second.jar", read_jar(source)))
            )
            assert not duplicate.valid and any(
                issue.code == "native_duplicate" for issue in duplicate.issues
            )
            altered = io.BytesIO(shared)
            with zipfile.ZipFile(altered, "a") as archive:
                archive.writestr("different.txt", "different nested bytes with the same version")
            with zipfile.ZipFile(root / "second.jar", "w") as archive:
                archive.writestr(
                    "fabric.mod.json",
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "id": "second",
                            "version": "1.0.0",
                            "jars": [{"file": "nested/library.jar"}],
                        }
                    ),
                )
                archive.writestr("nested/library.jar", altered.getvalue())
            ambiguous = validate_native(
                installation, (embedded[0], ("second.jar", read_jar(root / "second.jar")))
            )
            assert not ambiguous.valid and any(
                issue.code == "native_nested_unverified" for issue in ambiguous.issues
            )
            external = game / "mods/dependent.jar"
            with zipfile.ZipFile(external, "w") as archive:
                archive.writestr(
                    "fabric.mod.json",
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "id": "dependent",
                            "version": "1.0.0",
                            "depends": {"library": "*"},
                        }
                    ),
                )
            removal = manager.plan_removal((reference,))
            assert not removal.applicable and any(
                issue.code == "native_required" for issue in removal.issues
            )
            assert external.exists() and target.exists()
            external.unlink()
            original = source.read_bytes()
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(
                    "fabric.mod.json",
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "id": "library",
                            "version": "1.0.0",
                            "depends": {"minecraft": ">=1.22.0"},
                        }
                    ),
                )
            changed_file = replace(file, size=source.stat().st_size)
            changed = replace(
                mod, file=changed_file, version=replace(mod.version, files=(changed_file,))
            )
            blocked = manager.plan(
                replace(resolution, mods=(changed,)), (InstallInput(changed, source),)
            )
            assert not blocked.applicable and any(
                issue.code == "native_constraint" for issue in blocked.issues
            )
            source.write_bytes(original)
        removal = manager.plan_removal((reference,))
        assert removal.applicable, removal.issues
        manager.remove(removal, removal.fingerprint)
        assert not target.exists() and not manager.installed()

    class Process:
        pid = 99999999

        def name(self):
            return "java.exe"

        def cmdline(self):
            return ["java", "--gameDir", "minecraft"]

        def cwd(self):
            return str(root / "fabric")

    iterator = processes_engine.psutil.process_iter
    processes_engine.psutil.process_iter = lambda: iter((Process(),))
    try:
        assert not java_guard(root / "fabric/minecraft").valid
        assert java_guard(root / "different").valid
    finally:
        processes_engine.psutil.process_iter = iterator


async def main() -> None:
    logging.disable(logging.CRITICAL)
    with TemporaryDirectory(prefix="modmesh-safety-") as name:
        await asyncio.wait_for(check_downloads(Path(name)), timeout=20)
        await asyncio.wait_for(check_linked_downloads(Path(name)), timeout=20)
        await asyncio.wait_for(check_cache(Path(name)), timeout=20)
        await check_processes(Path(name) / "processes")
        check_planning(Path(name))
        check_installation(Path(name) / "installation")
        check_shared_dependencies(Path(name) / "shared")
        check_minecraft(Path(name) / "minecraft")
        check_profiles(Path(name) / "profiles")
        check_manifests(Path(name) / "manifests")
        await check_pack_import(Path(name) / "pack-import")
    print("Download, cache, plan and installation/recovery safety checks passed")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--install-crash":
        install_crash(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "--removal-crash":
        removal_crash(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "--profile-crash":
        profile_crash(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == "--cache-worker":
        asyncio.run(cache_worker(Path(sys.argv[2])))
    else:
        asyncio.run(main())
