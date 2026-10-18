from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from modmesh.core.conflicts import ConflictReport, analyze_dependencies
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.installation import InstallationManager, _check_cancelled
from modmesh.core.planning import FileSnapshot, snapshot, target_path
from modmesh.core.resolver import DependencyResolution, compatible
from modmesh.core.sources import SourceRegistry, VersionQuery
from modmesh.domain.models import InstalledMod, ModVersion, SourceRef
from modmesh.storage.installed import ManagedMod, array_record, mod_key, string_record


@dataclass(frozen=True)
class FileStatus:
    path: str
    state: str
    managed: bool
    owners: int


@dataclass(frozen=True)
class InventoryMod:
    installed: ManagedMod
    files: tuple[FileStatus, ...]


@dataclass(frozen=True)
class Inventory:
    managed: tuple[InventoryMod, ...]
    external: tuple[InstalledMod, ...]
    conflicts: ConflictReport


def inspect_inventory(
    manager: InstallationManager, cancelled: Callable[[], bool] | None = None
) -> Inventory:
    managed = []
    tracked = set()
    with manager._locked(cancelled=cancelled) as store:
        files = manager._file_records(store)
        mods = (
            tuple(ManagedMod.read(record) for _, record in store.list("install.mods"))
            if store
            else ()
        )
        for mod in mods:
            statuses = []
            for key in mod.paths:
                _check_cancelled(cancelled)
                record = files.get(key)
                if record is None or mod_key(mod.mod.project.reference) not in array_record(
                    record["owners"]
                ):
                    statuses.append(FileStatus(key, "missing_record", False, 0))
                    continue
                path = string_record(record["path"])
                tracked.add(path.casefold())
                state = "verified" if record["managed"] is True else "external"
                try:
                    target = target_path(manager.root, path)
                    if not target.exists():
                        state = "missing"
                    else:
                        current = snapshot(target, cancelled)
                        proof = FileSnapshot(
                            string_record(record["sha256"]),
                            int(str(record["size"])),
                            int(str(record["device"])),
                            int(str(record["inode"])),
                        )
                        if current != proof:
                            state = "modified"
                except OSError:
                    state = "unreadable"
                except ModMeshError as exc:
                    if exc.code == ErrorCode.CANCELLED:
                        raise
                    state = "unsafe_path"
                statuses.append(
                    FileStatus(
                        path, state, record["managed"] is True, len(array_record(record["owners"]))
                    )
                )
            managed.append(InventoryMod(mod, tuple(statuses)))
        _check_cancelled(cancelled)
        external = []
        for external_mod in manager.games.get(manager.installation.game_id).scan_mods(
            manager.installation
        ):
            remaining = tuple(
                file for file in external_mod.files if file.path.casefold() not in tracked
            )
            if remaining:
                external.append(replace(external_mod, files=remaining))
        installation = manager.installation
        resolution = DependencyResolution(
            installation.game_id,
            installation.version.value,
            installation.loader.name if installation.loader else None,
            tuple(mod.mod for mod in mods),
            (),
        )
        return Inventory(tuple(managed), tuple(external), analyze_dependencies(resolution))


@dataclass(frozen=True)
class UpdateCheck:
    reference: SourceRef
    state: str
    alternatives: tuple[ModVersion, ...] = ()
    message: str | None = None


async def check_updates(
    registry: SourceRegistry,
    mods: Sequence[ManagedMod],
    *,
    game_id: str,
    game_version: str | None,
    loader: str | None,
) -> tuple[UpdateCheck, ...]:
    semaphore = asyncio.Semaphore(4)

    async def check(mod: ManagedMod) -> UpdateCheck:
        reference = mod.mod.project.reference
        async with semaphore:
            try:
                async with asyncio.timeout(35):
                    source = registry.get(reference.source_id)
                    versions = await source.versions(
                        game_id,
                        reference.project_id,
                        query=VersionQuery(game_version, loader)
                        if source.version_filters
                        else None,
                    )
                alternatives = tuple(
                    version
                    for version in versions
                    if version.version_id != mod.mod.version.version_id
                    and compatible(version, game_version, loader)
                )
                return UpdateCheck(
                    reference,
                    "checked",
                    alternatives,
                    "출처 목록의 다른 호환 버전입니다. 자동 업그레이드 순서가 아닙니다.",
                )
            except (ModMeshError, TimeoutError) as exc:
                return UpdateCheck(
                    reference,
                    "unavailable",
                    message=exc.message
                    if isinstance(exc, ModMeshError)
                    else "Version lookup timed out",
                )

    return tuple(await asyncio.gather(*(check(mod) for mod in mods)))
