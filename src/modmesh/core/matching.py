"""Confirmed identity grouping and explicitly unconfirmed cross-source candidates."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.identity import EvidenceKind, IdentityEvidence, IdentityRegistry
from modmesh.domain.models import SourceProject, SourceRef


@dataclass(frozen=True)
class ProjectGroup:
    group_id: str
    canonical_id: str | None
    projects: tuple[SourceProject, ...]


@dataclass(frozen=True)
class MatchCandidate:
    game_id: str
    left: SourceRef
    right: SourceRef
    reason: str
    evidence: str | None = None
    requires_confirmation: bool = True


@dataclass(frozen=True)
class MatchingResult:
    groups: tuple[ProjectGroup, ...]
    candidates: tuple[MatchCandidate, ...]


def _pair_key(game_id: str, left: SourceRef, right: SourceRef) -> str:
    refs = sorted(((left.source_id, left.project_id), (right.source_id, right.project_id)))
    return json.dumps([game_id, *refs], separators=(",", ":"))


def _name(value: str) -> str:
    return re.sub(r"[\W_]+", "", value[:256].casefold())


def _repositories(project: SourceProject) -> set[str]:
    repositories: set[str] = set()
    for value in project.links:
        url = urlsplit(value)
        parts = url.path.strip("/").split("/")
        if url.scheme == "https" and url.hostname == "github.com" and len(parts) >= 2:
            repositories.add(
                "https://github.com/" + "/".join(parts[:2]).removesuffix(".git").casefold()
            )
    return repositories


class ProjectMatcher:
    def __init__(self, identities: IdentityRegistry) -> None:
        self.identities = identities

    def match(self, projects: Sequence[SourceProject]) -> MatchingResult:
        if len(projects) > 1000:
            raise ModMeshError(ErrorCode.VALIDATION, "Too many projects for one matching page")
        groups: dict[str, list[SourceProject]] = {}
        canonical: dict[str, str | None] = {}
        project_groups: dict[tuple[str, SourceRef], str] = {}
        for project in projects:
            known = self.identities.resolve(project.game_id, project.reference)
            key = known or json.dumps(
                [project.game_id, project.reference.source_id, project.reference.project_id],
                separators=(",", ":"),
            )
            groups.setdefault(key, []).append(project)
            canonical[key] = known
            project_groups[(project.game_id, project.reference)] = key
        rejected = {key for key, _ in self.identities.store.list("identity.exclusions")}
        candidates: list[MatchCandidate] = []
        names = [_name(project.name) for project in projects]
        repositories = [_repositories(project) for project in projects]
        authors = [{name.casefold() for name in project.authors} for project in projects]
        for index, left in enumerate(projects):
            for other in range(index + 1, len(projects)):
                right = projects[other]
                if (
                    left.game_id != right.game_id
                    or left.reference.source_id == right.reference.source_id
                ):
                    continue
                if (
                    project_groups[(left.game_id, left.reference)]
                    == project_groups[(right.game_id, right.reference)]
                ):
                    continue
                if _pair_key(left.game_id, left.reference, right.reference) in rejected:
                    continue
                if not names[index] or names[index] != names[other]:
                    continue
                shared = repositories[index] & repositories[other]
                if shared:
                    candidates.append(
                        MatchCandidate(
                            left.game_id,
                            left.reference,
                            right.reference,
                            "matching_name_and_author_provided_repository",
                            sorted(shared)[0],
                        )
                    )
                elif authors[index] & authors[other]:
                    candidates.append(
                        MatchCandidate(
                            left.game_id,
                            left.reference,
                            right.reference,
                            "matching_name_and_author_label",
                        )
                    )
        return MatchingResult(
            tuple(
                ProjectGroup(key, canonical[key], tuple(values)) for key, values in groups.items()
            ),
            tuple(candidates),
        )

    def confirm(self, left: SourceProject, right: SourceProject, evidence: IdentityEvidence) -> str:
        if left.game_id != right.game_id:
            raise ModMeshError(ErrorCode.CONFLICT, "Cannot match projects from different games")
        if evidence.kind == EvidenceKind.NATIVE_ID:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Cross-source confirmation needs explicit evidence"
            )
        left_id = self.identities.resolve(left.game_id, left.reference)
        right_id = self.identities.resolve(right.game_id, right.reference)
        if left_id is not None and right_id is not None and left_id != right_id:
            raise ModMeshError(
                ErrorCode.CONFLICT,
                "Projects already have different mappings; separate the intended alias first",
            )
        target = left_id or right_id or self.identities.get_or_create(left.game_id, left.reference)
        for project in (left, right):
            if self.identities.resolve(project.game_id, project.reference) != target:
                self.identities.bind(target, project.game_id, project.reference, evidence)
        with self.identities.store.transaction(write=True) as transaction:
            transaction.delete(
                "identity.exclusions", _pair_key(left.game_id, left.reference, right.reference)
            )
        return target

    def reject_candidate(self, game_id: str, left: SourceRef, right: SourceRef) -> None:
        left_id = self.identities.resolve(game_id, left)
        if left_id is not None and left_id == self.identities.resolve(game_id, right):
            raise ModMeshError(
                ErrorCode.CONFLICT, "Unbind a confirmed alias before rejecting its candidate"
            )
        self.identities.store.put(
            "identity.exclusions",
            _pair_key(game_id, left, right),
            {"reason": "explicit_user_separation"},
        )
