from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from modmesh.apps.runtime import create_game_registry, download_domains, source_priorities
from modmesh.core.alternatives import find_alternatives
from modmesh.core.artifact_matching import ArtifactCandidate
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings
from modmesh.core.conflicts import analyze_dependencies
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadResult, _blocking
from modmesh.core.identity import IdentityRegistry
from modmesh.core.installation import InstallationManager
from modmesh.core.local_files import copy_local
from modmesh.core.pack_io import resolve_pack, validate_context
from modmesh.core.pack_versions import save_version
from modmesh.core.packs import PackManifest
from modmesh.core.planning import InstallationPlan, InstallInput
from modmesh.core.profiles import ProfileManager, ProfilePlan
from modmesh.core.resolver import DependencyResolution, ResolvedMod
from modmesh.core.sources import SourceRegistry
from modmesh.domain.models import GameInstallation, SourceRef
from modmesh.storage.metadata import MetadataStore


@dataclass
class PreparedInstallation:
    plan: InstallationPlan
    manager: InstallationManager
    temporary: TemporaryDirectory[str]
    replace_existing: bool
    source_notes: tuple[str, ...] = ()

    def close(self) -> None:
        self.temporary.cleanup()


async def prepare_resolution(
    settings: ClientSettings,
    registry: SourceRegistry,
    installation: GameInstallation,
    resolution: DependencyResolution,
    *,
    rule: str | None = None,
    recipes: Mapping[SourceRef, tuple[str | None, str]] | None = None,
    profile_payload: bool = False,
    prefix: str = "",
    replace_existing: bool = False,
    cancelled: threading.Event | None = None,
    progress: Callable[[object], None] | None = None,
) -> PreparedInstallation:
    report = analyze_dependencies(resolution)
    if report.blocked:
        raise ModMeshError(
            ErrorCode.CONFLICT,
            "; ".join(issue.message for issue in report.issues if issue.blocking),
        )
    cancellation = cancelled or threading.Event()
    temporary = TemporaryDirectory(prefix="modmesh-plan-")
    try:
        inputs = []
        source_notes: list[str] = []
        with ArtifactCache(settings.paths.cache_dir, max_bytes=settings.cache_max_bytes) as cache:
            async with DownloadManager(
                download_domains(registry),
                policy=DownloadPolicy(max_bytes=min(8 * 1024**3, settings.cache_max_bytes)),
            ) as downloads:
                for index, mod in enumerate(resolution.mods):

                    async def fetch(
                        destination: Path, selected: ResolvedMod = mod
                    ) -> DownloadResult:
                        local = registry.get(selected.file.project.source_id).local_file(
                            selected.file
                        )
                        if local is not None:
                            return await _blocking(
                                partial(
                                    copy_local,
                                    selected.file,
                                    local,
                                    destination,
                                    cancellation,
                                    min(8 * 1024**3, settings.cache_max_bytes),
                                ),
                                cancellation,
                            )
                        requested = ArtifactCandidate(
                            selected.project,
                            selected.version,
                            selected.file,
                            resolution.game_version,
                            resolution.loader,
                        )
                        with MetadataStore(settings.paths.database) as store:
                            alternatives = await find_alternatives(
                                requested, registry, IdentityRegistry(store)
                            )
                        source_notes.extend(alternatives.notes)
                        result = await downloads.submit_candidate(
                            requested,
                            alternatives.candidates,
                            registry,
                            source_priorities(settings.source_priorities),
                            destination,
                            progress=progress,
                        ).wait()
                        source_notes.append(
                            f"{selected.project.name}: downloaded via {result.download_source_id}"
                        )
                        return result

                    entry = await cache.obtain(mod.file, fetch)
                    path = await cache.export_async(entry, Path(temporary.name) / str(index))
                    recipe = (recipes or {}).get(mod.project.reference)
                    inputs.append(
                        InstallInput(
                            mod,
                            path,
                            recipe[0] if recipe else (rule if mod.direct else None),
                            recipe[1] if recipe else (prefix if mod.direct else ""),
                        )
                    )
        if progress is not None:
            progress("검증한 파일의 변경 계획을 작성합니다.")
        manager = InstallationManager(create_game_registry(), installation)
        if profile_payload:
            manager = ProfileManager(manager.games, installation)
            plan = await _blocking(
                partial(
                    manager.payload_plan, resolution, tuple(inputs), cancelled=cancellation.is_set
                ),
                cancellation,
            )
            return PreparedInstallation(plan, manager, temporary, False, tuple(source_notes))
        plan = await _blocking(
            partial(
                manager.plan,
                resolution,
                inputs,
                replace_existing=replace_existing,
                cancelled=cancellation.is_set,
            ),
            cancellation,
        )
        return PreparedInstallation(plan, manager, temporary, replace_existing, tuple(source_notes))
    except BaseException:
        temporary.cleanup()
        raise


async def prepare_pack(
    settings: ClientSettings,
    registry: SourceRegistry,
    installation: GameInstallation,
    manifest: PackManifest,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[object], None] | None = None,
) -> PreparedInstallation:
    validate_context(manifest, installation)
    with MetadataStore(settings.paths.database) as store:
        resolution = await resolve_pack(
            manifest,
            registry,
            identities=IdentityRegistry(store),
            priorities=source_priorities(settings.source_priorities),
        )
    if not resolution.mods:
        raise ModMeshError(ErrorCode.VALIDATION, "Empty manifest has no installation changes")
    prepared = await prepare_resolution(
        settings,
        registry,
        installation,
        resolution,
        recipes={
            entry.mod.project.reference: (entry.rule_id, entry.prefix) for entry in manifest.entries
        },
        cancelled=cancelled,
        progress=progress,
    )
    installed = prepared.manager.installed()
    selected = {mod.project.reference for mod in resolution.mods}
    if any(item.mod.project.reference not in selected for item in installed):
        prepared.close()
        raise ModMeshError(
            ErrorCode.CONFLICT, "Select an empty profile before importing a different mod set"
        )
    prepared.source_notes += manifest.warnings
    return prepared


@dataclass(frozen=True)
class PreparedPackUpdate:
    manager: ProfileManager
    plan: ProfilePlan
    notes: tuple[str, ...] = ()


async def prepare_pack_update(
    settings: ClientSettings,
    registry: SourceRegistry,
    installation: GameInstallation,
    manifest: PackManifest,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[object], None] | None = None,
) -> PreparedPackUpdate:
    validate_context(manifest, installation)
    cancellation = cancelled or threading.Event()
    with MetadataStore(settings.paths.database) as store:
        resolution = await resolve_pack(
            manifest,
            registry,
            identities=IdentityRegistry(store),
            priorities=source_priorities(settings.source_priorities),
        )
    prepared = await prepare_resolution(
        settings,
        registry,
        installation,
        resolution,
        recipes={
            entry.mod.project.reference: (entry.rule_id, entry.prefix) for entry in manifest.entries
        },
        profile_payload=True,
        cancelled=cancellation,
        progress=progress,
    )
    try:
        manager = ProfileManager(prepared.manager.games, installation)
        save_version(manager, manifest)
        plan = await _blocking(
            partial(
                manager.stage_payload,
                prepared.plan,
                (manifest.name + f" r{manifest.revision}")[:100],
            ),
            cancellation,
        )
        return PreparedPackUpdate(manager, plan, manifest.warnings)
    finally:
        prepared.close()
