from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from modmesh.apps.runtime import DOWNLOAD_DOMAINS, create_game_registry, source_priorities
from modmesh.core.artifact_matching import ArtifactCandidate
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings
from modmesh.core.conflicts import analyze_dependencies
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadResult, _blocking
from modmesh.core.installation import InstallationManager
from modmesh.core.planning import InstallationPlan, InstallInput
from modmesh.core.resolver import DependencyResolution, ResolvedMod
from modmesh.core.sources import SourceRegistry
from modmesh.domain.models import GameInstallation


@dataclass
class PreparedInstallation:
    plan: InstallationPlan
    manager: InstallationManager
    temporary: TemporaryDirectory[str]
    replace_existing: bool

    def close(self) -> None:
        self.temporary.cleanup()


async def prepare_resolution(
    settings: ClientSettings,
    registry: SourceRegistry,
    installation: GameInstallation,
    resolution: DependencyResolution,
    *,
    rule: str | None = None,
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
        with ArtifactCache(settings.paths.cache_dir, max_bytes=settings.cache_max_bytes) as cache:
            async with DownloadManager(
                DOWNLOAD_DOMAINS,
                policy=DownloadPolicy(max_bytes=min(8 * 1024**3, settings.cache_max_bytes)),
            ) as downloads:
                for index, mod in enumerate(resolution.mods):

                    async def fetch(
                        destination: Path, selected: ResolvedMod = mod
                    ) -> DownloadResult:
                        return await downloads.submit_candidate(
                            ArtifactCandidate(
                                selected.project,
                                selected.version,
                                selected.file,
                                resolution.game_version,
                                resolution.loader,
                            ),
                            (),
                            registry,
                            source_priorities(settings.source_priorities),
                            destination,
                            progress=progress,
                        ).wait()

                    entry = await cache.obtain(mod.file, fetch)
                    path = await cache.export_async(entry, Path(temporary.name) / str(index))
                    inputs.append(
                        InstallInput(
                            mod, path, rule if mod.direct else None, prefix if mod.direct else ""
                        )
                    )
        if progress is not None:
            progress("검증한 파일의 변경 계획을 작성합니다.")
        manager = InstallationManager(create_game_registry(), installation)
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
        return PreparedInstallation(plan, manager, temporary, replace_existing)
    except BaseException:
        temporary.cleanup()
        raise
