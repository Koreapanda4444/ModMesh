import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from modmesh.apps.guarded_launch import guarded_launch
from modmesh.apps.launch_planning import prepare_launch
from modmesh.apps.runtime import download_domains, source_registry
from modmesh.apps.workspace_activation import (
    WorkspaceActivation,
    apply_workspace,
    prepare_locked_workspace,
)
from modmesh.apps.workspace_artifacts import artifact_vault
from modmesh.apps.workspace_lifecycle import freeze_workspace
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadResult, _blocking
from modmesh.core.installation import OperationProgress, _check_cancelled
from modmesh.core.launching import LaunchBoundary, LaunchPlan
from modmesh.core.local_files import copy_local
from modmesh.core.lockfiles import PackLock
from modmesh.core.sources import DownloadMode, SourceAdapter, SourceCapability, SourceRegistry
from modmesh.core.workspaces import document_digest
from modmesh.domain.models import GameInstallation, ModFile


@dataclass(frozen=True)
class PlayPlan:
    activation: WorkspaceActivation
    launch: LaunchPlan

    @property
    def fingerprint(self) -> str:
        return document_digest(
            {"activation": self.activation.fingerprint, "launch": self.launch.fingerprint}
        )

    def document(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "activation": self.activation.document(),
            "launch": self.launch.document(),
            "mod_loading": "unverified",
        }


async def acquire_play_artifacts(
    settings: ClientSettings,
    lock: PackLock,
    registry: SourceRegistry,
    cancelled: threading.Event,
    progress: Callable[[object], None] | None = None,
) -> None:
    limit = min(8 * 1024**3, settings.cache_max_bytes)
    with (
        artifact_vault(settings) as vault,
        ArtifactCache(settings.paths.cache_dir, max_bytes=settings.cache_max_bytes) as cache,
    ):
        async with DownloadManager(
            download_domains(registry), policy=DownloadPolicy(max_bytes=limit)
        ) as downloads:
            for entry in lock.entries:
                _check_cancelled(cancelled.is_set)
                file = entry.mod.file
                available = await _blocking(
                    partial(vault.lookup, file, cancelled=cancelled), cancelled
                )
                if available is not None:
                    continue
                if file.project.source_id == "local":
                    raise ModMeshError(
                        ErrorCode.NOT_FOUND, "Reimport the missing local artifact before Play"
                    )
                source = registry.get(file.project.source_id)

                async def fetch(
                    destination: Path, file: ModFile = file, source: SourceAdapter = source
                ) -> DownloadResult:
                    _check_cancelled(cancelled.is_set)
                    source.require(lock.context.game_id, SourceCapability.DOWNLOAD_DIRECT)
                    local = source.local_file(file)
                    if local is not None:
                        return await _blocking(
                            partial(copy_local, file, local, destination, cancelled, limit),
                            cancelled,
                        )
                    access = await source.resolve_download(lock.context.game_id, file)
                    if access.mode != DownloadMode.DIRECT:
                        raise ModMeshError(
                            ErrorCode.UNSUPPORTED,
                            "This exact file requires manual acquisition from its source",
                        )
                    handle = downloads.submit(file, access, destination, progress=progress)
                    task = asyncio.create_task(handle.wait())
                    try:
                        while not task.done():
                            _check_cancelled(cancelled.is_set)
                            await asyncio.wait((task,), timeout=0.05)
                        return await task
                    finally:
                        if not task.done():
                            handle.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                await cache.obtain(file, fetch)
                if progress:
                    progress(f"검증한 파일 준비: {file.filename}")


async def prepare_play(
    settings: ClientSettings,
    pack_id: str,
    installation: GameInstallation,
    expected: str,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[object], None] | None = None,
) -> PlayPlan:
    cancellation = cancelled or threading.Event()
    launch = await _blocking(partial(prepare_launch, settings, installation), cancellation)
    lock = await _blocking(
        partial(freeze_workspace, settings, pack_id, installation, expected), cancellation
    )
    if progress:
        progress("잠금 버전의 파일과 다운로드 권한을 확인합니다.")
    async with source_registry(database=settings.paths.database) as registry:
        await acquire_play_artifacts(settings, lock, registry, cancellation, progress)
    activation = await prepare_locked_workspace(
        settings, lock, installation, cancelled=cancellation
    )
    return PlayPlan(activation, launch)


def apply_play(
    settings: ClientSettings,
    plan: PlayPlan,
    approval: str,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[OperationProgress], None] | None = None,
    boundary: LaunchBoundary | None = None,
) -> dict[str, object]:
    cancellation = cancelled or threading.Event()
    if approval != plan.fingerprint:
        raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact Play file and launch plan")
    installation = plan.activation.manager.installation
    if prepare_launch(settings, installation).fingerprint != plan.launch.fingerprint:
        raise ModMeshError(ErrorCode.CONFLICT, "Launch changed; review a new Play plan")
    _check_cancelled(cancellation.is_set)
    receipt = apply_workspace(
        settings,
        plan.activation,
        plan.activation.fingerprint,
        cancelled=cancellation,
        progress=progress,
    )
    _check_cancelled(cancellation.is_set)
    return guarded_launch(
        settings,
        installation,
        plan.launch,
        receipt,
        plan.launch.fingerprint,
        cancelled=cancellation,
        boundary=boundary,
    )
