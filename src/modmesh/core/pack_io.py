from __future__ import annotations

import asyncio
import os
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.identity import IdentityRegistry
from modmesh.core.packs import MAX_MANIFEST_BYTES, PackContext, PackEntry, PackManifest, PackSetting
from modmesh.core.priorities import SourcePriorities
from modmesh.core.profiles import ProfileManager
from modmesh.core.resolver import DependencyResolution, DependencyResolver, ResolvedMod, compatible
from modmesh.core.sources import SourceCapability, SourceRegistry, VersionQuery
from modmesh.domain.models import GameInstallation
from modmesh.storage.installed import object_record


def export_profile(manager: ProfileManager, profile_id: str) -> PackManifest:
    profile = next((item for item in manager.profiles() if item.profile_id == profile_id), None)
    if profile is None:
        raise ModMeshError(ErrorCode.NOT_FOUND, "Select a saved local profile")
    if profile_id == manager.active_id():
        profile = manager.capture_active()
    version = object_record(profile.context["version"])
    context = PackContext.read(
        {
            "game_id": profile.context["game_id"],
            "edition": profile.context["edition"],
            "platform": profile.context["platform"],
            "game_version": version.get("value"),
            "build_id": version.get("build_id"),
            "loader": profile.context["loader"],
        }
    )
    return PackManifest.create(
        profile.name,
        context,
        tuple(PackEntry(item.mod, item.rule_id, item.prefix) for item in profile.mods),
        tuple(PackSetting(item.path, item.sha256) for item in profile.files if item.setting),
    )


def write_manifest(path: Path, manifest: PackManifest) -> None:
    payload = manifest.dumps()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        if stream.write(payload) != len(payload):
            raise ModMeshError(ErrorCode.STORAGE, "Manifest export was incomplete")
        stream.flush()
        os.fsync(stream.fileno())


def read_manifest(path: Path) -> PackManifest:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ModMeshError(ErrorCode.VALIDATION, "Manifest must be a bounded regular JSON file")
    with path.open("rb") as stream:
        return PackManifest.loads(stream.read(MAX_MANIFEST_BYTES + 1))


def validate_context(manifest: PackManifest, installation: GameInstallation) -> None:
    context = PackContext.from_installation(installation)
    expected = manifest.context
    if (expected.game_id, expected.edition, expected.platform) != (
        context.game_id,
        context.edition,
        context.platform,
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Pack is for another game, edition or platform")
    for wanted, actual in (
        (expected.game_version, context.game_version),
        (expected.build_id, context.build_id),
        (expected.loader, context.loader),
    ):
        if wanted != actual:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Pack needs a different game version/build or loader"
            )


async def resolve_pack(
    manifest: PackManifest,
    registry: SourceRegistry,
    *,
    identities: IdentityRegistry | None = None,
    priorities: SourcePriorities | None = None,
) -> DependencyResolution:
    semaphore = asyncio.Semaphore(4)

    async def lookup(entry: PackEntry) -> ResolvedMod:
        async with semaphore:
            async with asyncio.timeout(30):
                expected = entry.mod
                reference = expected.project.reference
                source = registry.get(reference.source_id)
                source.require(manifest.context.game_id, SourceCapability.VERSIONS)
                project = await source.project(manifest.context.game_id, reference.project_id)
                query = VersionQuery(
                    manifest.context.game_version,
                    manifest.context.loader.name if manifest.context.loader else None,
                )
                versions = await source.versions(
                    manifest.context.game_id,
                    reference.project_id,
                    query=query if source.version_filters else None,
                )
                if len(versions) > 2000:
                    raise ModMeshError(ErrorCode.UNSUPPORTED, "Pack version catalog limit reached")
                matches = tuple(
                    value for value in versions if value.version_id == expected.version.version_id
                )
                if (
                    project.reference != reference
                    or project.game_id != manifest.context.game_id
                    or len(matches) != 1
                ):
                    raise ModMeshError(
                        ErrorCode.INTEGRITY, "Pinned pack project or version is unavailable"
                    )
                version = matches[0]
                files = tuple(
                    value for value in version.files if value.file_id == expected.file.file_id
                )
                if version.project != reference or len(files) != 1:
                    raise ModMeshError(ErrorCode.INTEGRITY, "Pinned pack file is unavailable")
                file = files[0]
                hashes = {value.algorithm: value for value in file.hashes}
                if (
                    not expected.file.hashes
                    or file.filename != expected.file.filename
                    or file.size != expected.file.size
                    or any(hashes.get(value.algorithm) != value for value in expected.file.hashes)
                ):
                    raise ModMeshError(
                        ErrorCode.INTEGRITY,
                        "Pinned pack file metadata changed or has no verifiable hash",
                    )
                if (
                    version.dependencies != expected.version.dependencies
                    or version.dependencies_known != expected.version.dependencies_known
                ):
                    raise ModMeshError(
                        ErrorCode.INTEGRITY, "Pinned pack dependency metadata changed"
                    )
                if not compatible(version, query.game_version, query.loader):
                    raise ModMeshError(ErrorCode.CONFLICT, "Pinned pack file is incompatible")
                return ResolvedMod(project, version, file, expected.direct)

    mods = tuple(await asyncio.gather(*(lookup(entry) for entry in manifest.entries)))
    if not mods:
        return DependencyResolution(
            manifest.context.game_id,
            manifest.context.game_version,
            manifest.context.loader.name if manifest.context.loader else None,
            (),
            (),
        )
    resolution = await DependencyResolver(
        registry, identities=identities, priorities=priorities
    ).resolve(
        manifest.context.game_id,
        mods,
        game_version=manifest.context.game_version,
        loader=manifest.context.loader.name if manifest.context.loader else None,
    )
    expected_files = {
        (entry.mod.project.reference, entry.mod.version.version_id, entry.mod.file.file_id)
        for entry in manifest.entries
    }
    actual = {
        (mod.project.reference, mod.version.version_id, mod.file.file_id) for mod in resolution.mods
    }
    if not resolution.solved or actual != expected_files:
        raise ModMeshError(
            ErrorCode.CONFLICT, "Manifest does not pin a complete satisfiable dependency set"
        )
    return resolution
