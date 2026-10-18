from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from modmesh.core.artifact_matching import ArtifactCandidate, identical_candidates
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.identity import IdentityRegistry
from modmesh.core.resolver import DependencyResolution, ResolvedMod, matches_constraint
from modmesh.domain.dependencies import Dependency, DependencyKind
from modmesh.domain.models import SourceRef


@dataclass(frozen=True)
class DependencyConflict:
    code: str
    message: str
    projects: tuple[SourceRef, ...] = ()
    blocking: bool = True


@dataclass(frozen=True)
class ConflictReport:
    issues: tuple[DependencyConflict, ...]

    @property
    def blocked(self) -> bool:
        return any(issue.blocking for issue in self.issues)


def dependency_matches(
    declaration: Dependency, mod: ResolvedMod, identities: IdentityRegistry | None = None
) -> bool:
    target = declaration.target
    reference = mod.project.reference
    if target.source_id is not None and target.source_id != reference.source_id:
        return False
    if target.project_id is not None and target.project_id != reference.project_id:
        return False
    if target.version_id is not None and target.version_id != mod.version.version_id:
        return False
    if target.canonical_id is not None and (
        identities is None
        or identities.resolve(mod.project.game_id, reference) != target.canonical_id
    ):
        return False
    return bool(target.project_id or target.version_id or target.canonical_id)


def analyze_dependencies(
    resolution: DependencyResolution,
    installed: Sequence[ResolvedMod] = (),
    *,
    identities: IdentityRegistry | None = None,
) -> ConflictReport:
    if len(resolution.mods) + len(installed) > 512:
        raise ModMeshError(ErrorCode.VALIDATION, "Too many mods for dependency analysis")
    issues: list[DependencyConflict] = []
    selected: dict[SourceRef, ResolvedMod] = {}

    def add(
        code: str, message: str, refs: tuple[SourceRef, ...] = (), blocking: bool = True
    ) -> None:
        issue = DependencyConflict(code, message, refs, blocking)
        if issue not in issues:
            issues.append(issue)

    for problem in resolution.issues:
        add(problem.code, problem.message, (problem.parent,) if problem.parent else ())
    for mod in (*installed, *resolution.mods):
        reference = mod.project.reference
        previous = selected.get(reference)
        if previous is not None and (
            previous.version.version_id != mod.version.version_id or previous.file != mod.file
        ):
            add(
                "multiple_versions",
                "A project has multiple selected or installed versions",
                (reference,),
            )
        selected[reference] = mod
        if mod.project.game_id != resolution.game_id:
            add("wrong_game", "Mod belongs to a different game", (reference,))
        version = mod.version
        if resolution.game_version is None or not version.game_versions:
            add(
                "unknown_game_version",
                "Game-version compatibility is not verified",
                (reference,),
                False,
            )
        elif resolution.game_version not in version.game_versions:
            add(
                "game_version",
                "Mod does not declare support for the selected game version",
                (reference,),
            )
        if resolution.loader is None or not version.loaders:
            add("unknown_loader", "Loader compatibility is not verified", (reference,), False)
        elif resolution.loader not in version.loaders:
            add("loader", "Mod does not support the selected loader", (reference,))
        if not version.dependencies_known:
            add(
                "unknown_dependencies",
                "The source has not supplied dependency metadata",
                (reference,),
                False,
            )
    graph: dict[SourceRef, set[SourceRef]] = {ref: set() for ref in selected}
    for reference, mod in selected.items():
        for declaration in mod.version.dependencies:
            kind = declaration.kind
            matches = [
                candidate
                for candidate in selected.values()
                if dependency_matches(declaration, candidate, identities)
            ]
            if kind == DependencyKind.REQUIRED and not matches:
                add(
                    "missing_dependency",
                    "A required dependency is missing or cannot be identified",
                    (reference,),
                )
                continue
            if kind in {DependencyKind.TOOL, DependencyKind.INCLUDE}:
                add(
                    "unverified_relationship",
                    "Source tool/include relationship needs explicit interpretation",
                    (reference,),
                    False,
                )
                continue
            if kind == DependencyKind.EMBEDDED:
                if matches:
                    add(
                        "embedded_duplicate",
                        "An embedded dependency is also selected separately",
                        (reference, *(candidate.project.reference for candidate in matches)),
                        False,
                    )
                continue
            try:
                compatible = [
                    candidate
                    for candidate in matches
                    if matches_constraint(candidate.version, declaration.constraint)
                ]
            except ModMeshError as exc:
                add("unsupported_constraint", exc.message, (reference,))
                continue
            if kind == DependencyKind.INCOMPATIBLE:
                if (
                    declaration.game_versions
                    and resolution.game_version not in declaration.game_versions
                ):
                    continue
                if declaration.loaders and resolution.loader not in declaration.loaders:
                    continue
                if compatible:
                    add(
                        "incompatible",
                        "Explicitly incompatible mods are present together",
                        (reference, *(candidate.project.reference for candidate in compatible)),
                    )
                continue
            if matches and not compatible:
                add(
                    "dependency_version",
                    "Present dependency does not satisfy its version constraint",
                    (reference, *(candidate.project.reference for candidate in matches)),
                )
            if kind == DependencyKind.REQUIRED:
                if (
                    declaration.game_versions
                    and resolution.game_version not in declaration.game_versions
                ):
                    add(
                        "dependency_game_version",
                        "Dependency game-version condition is not met",
                        (reference,),
                    )
                if declaration.loaders and resolution.loader not in declaration.loaders:
                    add("dependency_loader", "Dependency loader condition is not met", (reference,))
                graph[reference].update(candidate.project.reference for candidate in compatible)
    colors: dict[SourceRef, int] = {}
    stack: list[SourceRef] = []

    def visit(reference: SourceRef) -> None:
        if len(stack) >= 128:
            add("graph_depth", "Dependency graph exceeds safe analysis depth", (reference,))
            return
        colors[reference] = 1
        stack.append(reference)
        for target in sorted(graph[reference]):
            if colors.get(target) == 1:
                cycle = tuple(stack[stack.index(target) :])
                add("dependency_cycle", "Required dependencies contain a cycle", cycle)
            elif colors.get(target, 0) == 0:
                visit(target)
        stack.pop()
        colors[reference] = 2

    for reference in graph:
        if colors.get(reference, 0) == 0:
            visit(reference)
    if identities is not None:
        groups: dict[str, list[ResolvedMod]] = {}
        for reference, mod in selected.items():
            canonical = identities.resolve(resolution.game_id, reference)
            if canonical is not None:
                groups.setdefault(canonical, []).append(mod)
        for members in groups.values():
            if len(members) < 2:
                continue
            candidates = [
                ArtifactCandidate(
                    member.project,
                    member.version,
                    member.file,
                    resolution.game_version,
                    resolution.loader,
                )
                for member in members
            ]
            identical = all(
                identical_candidates(candidates[0], member) for member in candidates[1:]
            )
            add(
                "canonical_duplicate",
                "The same confirmed mod is present from multiple sources",
                tuple(member.project.reference for member in members),
                not identical,
            )
    return ConflictReport(tuple(issues))
