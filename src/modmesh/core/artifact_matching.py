from collections.abc import Sequence
from dataclasses import dataclass

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.artifacts import FileHash
from modmesh.domain.models import ModFile, ModVersion, SourceProject


@dataclass(frozen=True)
class ArtifactCandidate:
    project: SourceProject
    version: ModVersion
    file: ModFile
    game_version: str | None
    loader: str | None

    def __post_init__(self) -> None:
        if (
            self.version.project != self.project.reference
            or self.file.project != self.project.reference
            or self.file not in self.version.files
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION,
                "Artifact candidate has mismatched project/version/file metadata",
            )

    @property
    def compatibility_verified(self) -> bool:
        return (
            self.game_version is not None
            and self.game_version in self.version.game_versions
            and self.loader is not None
            and self.loader in self.version.loaders
        )

    @property
    def context(self) -> tuple[str, str | None, str | None]:
        return self.project.game_id, self.game_version, self.loader


@dataclass(frozen=True)
class ArtifactGroup:
    candidates: tuple[ArtifactCandidate, ...]
    shared_expected_hashes: tuple[FileHash, ...]
    compatibility_verified: bool
    notes: tuple[str, ...] = ()


def _strong(candidate: ArtifactCandidate) -> set[FileHash]:
    return {value for value in candidate.file.hashes if value.strong}


def _metadata_conflicts(left: ArtifactCandidate, right: ArtifactCandidate) -> bool:
    hashes = {value.algorithm: value for value in left.file.hashes}
    if any(
        value.algorithm in hashes and hashes[value.algorithm] != value
        for value in right.file.hashes
    ):
        return True
    return (
        left.file.size is not None
        and right.file.size is not None
        and left.file.size != right.file.size
    )


def identical_candidates(left: ArtifactCandidate, right: ArtifactCandidate) -> bool:
    return (
        left.compatibility_verified
        and right.compatibility_verified
        and left.context == right.context
        and bool(_strong(left) & _strong(right))
        and not _metadata_conflicts(left, right)
    )


def merge_artifacts(candidates: Sequence[ArtifactCandidate]) -> tuple[ArtifactGroup, ...]:
    if len(candidates) > 2000:
        raise ModMeshError(ErrorCode.VALIDATION, "Too many artifact candidates for one page")
    groups: list[ArtifactGroup] = []
    notes: tuple[str, ...]
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.project.game_id,
            candidate.project.reference,
            candidate.file.file_id,
            candidate.version.version_id,
        ),
    )
    for candidate in ordered:
        strong = _strong(candidate)
        for index, group in enumerate(groups):
            if not candidate.compatibility_verified or not group.compatibility_verified:
                continue
            common = strong & set(group.shared_expected_hashes)
            if not common or any(
                not identical_candidates(member, candidate) for member in group.candidates
            ):
                continue
            members = (*group.candidates, candidate)
            notes = (
                ("Version labels differ; the requested version remains pinned.",)
                if len({member.version.number for member in members}) > 1
                else ()
            )
            groups[index] = ArtifactGroup(
                members, tuple(sorted(common, key=lambda value: value.algorithm.value)), True, notes
            )
            break
        else:
            notes = ()
            if not candidate.compatibility_verified:
                notes += (
                    "Game-version/loader compatibility is unverified; no cross-source merge.",
                )
            if not strong:
                notes += ("No strong source hash is available; no cross-source merge.",)
            groups.append(
                ArtifactGroup(
                    (candidate,),
                    tuple(sorted(strong, key=lambda value: value.algorithm.value)),
                    candidate.compatibility_verified,
                    notes,
                )
            )
    return tuple(groups)
