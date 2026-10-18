"""Game-scoped source order and permission-aware fallback for identical content."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from modmesh.core.artifact_matching import ArtifactCandidate, identical_candidates, merge_artifacts
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.sources import DownloadAccess, DownloadMode, SourceCapability, SourceRegistry
from modmesh.domain.artifacts import FileHash
from modmesh.domain.validation import require_id


class SourcePriorities:
    def __init__(self, priorities: Mapping[str, Sequence[str]] | None = None) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for game_id, values in (priorities or {}).items():
            require_id(game_id, "priority game ID")
            if isinstance(values, str) or len(set(values)) != len(values):
                raise ModMeshError(
                    ErrorCode.CONFIGURATION, "Source priorities must be an ordered unique list"
                )
            for source_id in values:
                require_id(source_id, "priority source ID")
            normalized[game_id] = tuple(values)
        self.values = MappingProxyType(normalized)

    def rank(self, game_id: str, source_id: str) -> tuple[int, str]:
        order = self.values.get(game_id, ())
        return (order.index(source_id) if source_id in order else len(order), source_id)


@dataclass(frozen=True)
class SourceAttempt:
    source_id: str
    state: str
    reason: str | None = None


@dataclass(frozen=True)
class DownloadChoice:
    requested: ArtifactCandidate
    download_from: ArtifactCandidate
    access: DownloadAccess
    attempts: tuple[SourceAttempt, ...]

    def __post_init__(self) -> None:
        if self.requested != self.download_from and not identical_candidates(
            self.requested, self.download_from
        ):
            raise ModMeshError(
                ErrorCode.INTEGRITY, "Download fallback is not a proven identical candidate"
            )

    @property
    def fallback(self) -> bool:
        return self.requested.file.project != self.download_from.file.project

    @property
    def verification_hashes(self) -> tuple[FileHash, ...]:
        hashes = {
            value.algorithm: value
            for candidate in (self.requested, self.download_from)
            for value in candidate.file.hashes
        }
        return tuple(sorted(hashes.values(), key=lambda value: value.algorithm.value))


async def choose_download(
    requested: ArtifactCandidate,
    alternatives: Sequence[ArtifactCandidate],
    registry: SourceRegistry,
    priorities: SourcePriorities,
    *,
    allow_fallback: bool = True,
    exclude_sources: frozenset[str] = frozenset(),
) -> DownloadChoice:
    pool = tuple(dict.fromkeys((requested, *alternatives))) if allow_fallback else (requested,)
    group = next(group for group in merge_artifacts(pool) if requested in group.candidates)
    ordered = sorted(
        group.candidates,
        key=lambda candidate: priorities.rank(
            requested.project.game_id, candidate.file.project.source_id
        ),
    )
    attempts: list[SourceAttempt] = []
    external: tuple[ArtifactCandidate, DownloadAccess] | None = None
    for candidate in ordered:
        key = candidate.file.project.source_id
        if key in exclude_sources:
            continue
        try:
            source = registry.get(key)
            status = source.status(requested.project.game_id)
            if not status.available or not status.capabilities & {
                SourceCapability.DOWNLOAD_DIRECT,
                SourceCapability.EXTERNAL_HANDOFF,
            }:
                attempts.append(SourceAttempt(key, "unavailable", status.reason))
                continue
            access = await source.resolve_download(requested.project.game_id, candidate.file)
            attempts.append(SourceAttempt(key, access.mode.value, access.reason))
            if access.mode == DownloadMode.DIRECT:
                return DownloadChoice(requested, candidate, access, tuple(attempts))
            if access.mode == DownloadMode.EXTERNAL and (
                external is None or candidate == requested
            ):
                external = candidate, access
        except ModMeshError as exc:
            attempts.append(SourceAttempt(key, "error", exc.message))
        except Exception as exc:
            attempts.append(
                SourceAttempt(key, "error", f"Source resolution error: {type(exc).__name__}")
            )
    if external is not None:
        return DownloadChoice(requested, external[0], external[1], tuple(attempts))
    raise ModMeshError(
        ErrorCode.NETWORK,
        "No permitted source can provide the requested file",
        details={
            "attempts": [
                {"source_id": item.source_id, "state": item.state, "reason": item.reason}
                for item in attempts
            ]
        },
    )
