from __future__ import annotations

import stat
import threading
import zipfile
from dataclasses import replace
from functools import partial
from pathlib import Path

from modmesh.apps.runtime import create_game_registry
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadResult, _blocking
from modmesh.core.games import GameCapability
from modmesh.core.hashing import verify_file
from modmesh.core.local_files import copy_local
from modmesh.core.packs import PackEntry
from modmesh.core.planning import ArchivePolicy, portable_path
from modmesh.core.resolver import ResolvedMod
from modmesh.core.workspaces import Workspace, WorkspaceStore
from modmesh.domain.models import ModFile, ModVersion, SourceProject, SourceRef
from modmesh.storage.metadata import MetadataStore


def artifact_vault(settings: ClientSettings) -> ArtifactCache:
    return ArtifactCache(
        settings.paths.data_dir / "workspace-artifacts", max_bytes=64 * 1024**3, evict=False
    )


def inspect_local(path: Path, *, filename: str | None = None) -> None:
    filename = filename or path.name
    portable_path(filename)
    if (
        path.is_symlink()
        or path.is_junction()
        or not path.is_file()
        or path.stat().st_size > 8 * 1024**3
    ):
        raise ModMeshError(ErrorCode.VALIDATION, "Select a regular local mod file below 8 GiB")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".zip", ".jar", ".dll", ".pak"}:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Local import accepts ZIP, JAR, DLL and Pak")
    if suffix in {".zip", ".jar"}:
        policy = ArchivePolicy()
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if (
                    len(infos) > policy.max_files
                    or sum(item.file_size for item in infos) > policy.max_bytes
                ):
                    raise ModMeshError(ErrorCode.VALIDATION, "Local archive exceeds import limits")
                seen = set()
                for info in infos:
                    name = portable_path(info.orig_filename.rstrip("/"))
                    if (
                        name.casefold() in seen
                        or info.flag_bits & 1
                        or stat.S_IFMT(info.external_attr >> 16)
                        not in {0, stat.S_IFREG, stat.S_IFDIR}
                    ):
                        raise ModMeshError(
                            ErrorCode.VALIDATION, "Linked, encrypted or duplicate archive entry"
                        )
                    seen.add(name.casefold())
                    if info.file_size > max(1, info.compress_size) * policy.max_ratio:
                        raise ModMeshError(ErrorCode.VALIDATION, "Archive expansion limit reached")
        except (OSError, zipfile.BadZipFile) as exc:
            raise ModMeshError(ErrorCode.INTEGRITY, "Local file is not a readable ZIP/JAR") from exc


async def import_local(
    settings: ClientSettings,
    pack_id: str,
    path: Path,
    expected: str,
    *,
    cancelled: threading.Event | None = None,
) -> Workspace:
    cancelled = cancelled or threading.Event()
    with MetadataStore(settings.paths.database) as database:
        workspace = WorkspaceStore(database).get(pack_id)
    if workspace.digest != expected:
        raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed before file import")
    adapter = create_game_registry(settings).get(workspace.game_id)
    if GameCapability.INSTALL_RULES not in adapter.capabilities:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "This game uses read-only platform-managed mods")
    path = path.expanduser().absolute()
    await _blocking(partial(inspect_local, path), cancelled)
    artifact = await _blocking(partial(verify_file, path, cancelled=cancelled.is_set), cancelled)
    reference = SourceRef("local", workspace.game_id + ":" + artifact.sha256.value)
    file = ModFile(
        reference,
        artifact.sha256.value,
        path.name,
        artifact.size,
        hashes=(artifact.sha256,),
        primary=True,
    )
    version = ModVersion(reference, artifact.sha256.value, None, files=(file,))
    mod = ResolvedMod(
        SourceProject(reference, workspace.game_id, path.stem[:200]), version, file, True
    )
    if any(entry.mod.project.reference == reference for entry in workspace.entries):
        raise ModMeshError(ErrorCode.CONFLICT, "This artifact is already in the workspace")
    with artifact_vault(settings) as vault:

        async def fetch(destination: Path) -> DownloadResult:
            return await _blocking(
                partial(copy_local, file, path, destination, cancelled, 8 * 1024**3), cancelled
            )

        retained = await vault.obtain(file, fetch)
        await _blocking(partial(inspect_local, retained.path, filename=file.filename), cancelled)
    if cancelled.is_set():
        raise ModMeshError(ErrorCode.CANCELLED, "Local import cancelled")
    with MetadataStore(settings.paths.database) as database:
        return WorkspaceStore(database).save(
            replace(
                workspace,
                revision=workspace.revision + 1,
                entries=(*workspace.entries, PackEntry(mod)),
            ),
            expected,
        )


def local_artifact(settings: ClientSettings, file: ModFile) -> Path:
    with artifact_vault(settings) as vault:
        entry = vault.lookup(file)
        if entry is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Retained artifact is missing; import it again")
        return entry.path
