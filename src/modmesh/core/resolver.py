from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from semantic_version import NpmSpec, SimpleSpec, Version

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.identity import IdentityRegistry
from modmesh.core.priorities import SourcePriorities
from modmesh.core.sources import SourceRegistry, VersionQuery
from modmesh.domain.dependencies import Dependency, DependencyKind, VersionConstraint
from modmesh.domain.models import ModFile, ModVersion, SourceProject, SourceRef


@dataclass(frozen=True)
class ResolvedMod:
    project: SourceProject
    version: ModVersion
    file: ModFile
    direct: bool = False

    def __post_init__(self) -> None:
        if self.project.reference != self.version.project or self.file not in self.version.files:
            raise ModMeshError(ErrorCode.VALIDATION, "Mismatched resolved mod metadata")


@dataclass(frozen=True)
class DependencyEdge:
    parent: SourceRef
    target: SourceRef
    declaration: Dependency


@dataclass(frozen=True)
class ResolutionIssue:
    code: str
    message: str
    parent: SourceRef | None = None
    dependency: Dependency | None = None


@dataclass(frozen=True)
class DependencyResolution:
    game_id: str
    game_version: str | None
    loader: str | None
    mods: tuple[ResolvedMod, ...]
    edges: tuple[DependencyEdge, ...]
    issues: tuple[ResolutionIssue, ...] = ()

    @property
    def solved(self) -> bool:
        return not self.issues

    @property
    def metadata_complete(self) -> bool:
        return all(mod.version.dependencies_known for mod in self.mods)


def primary_file(version: ModVersion) -> ModFile:
    files = version.files
    primary = tuple(file for file in files if file.primary)
    if len(files) == 1:
        return files[0]
    if len(primary) == 1:
        return primary[0]
    raise ModMeshError(ErrorCode.VALIDATION, "Select an exact file; no unique primary file exists")


def matches_constraint(version: ModVersion, constraint: VersionConstraint) -> bool:
    if constraint.exact_versions and version.version_id not in constraint.exact_versions:
        return False
    if constraint.expression is None:
        return True
    if constraint.scheme not in {"semver", "npm"}:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Unsupported dependency version scheme")
    try:
        spec = (NpmSpec if constraint.scheme == "npm" else SimpleSpec)(constraint.expression)
    except ValueError as exc:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid dependency version expression") from exc
    try:
        value = Version(version.number) if version.number is not None else None
    except ValueError:
        return False
    return value is not None and bool(spec.match(value))


def compatible(version: ModVersion, game_version: str | None, loader: str | None) -> bool:
    return (game_version is None or game_version in version.game_versions) and (
        loader is None or loader in version.loaders
    )


class DependencyResolver:
    def __init__(
        self,
        sources: SourceRegistry,
        *,
        identities: IdentityRegistry | None = None,
        priorities: SourcePriorities | None = None,
        max_nodes: int = 128,
        max_steps: int = 5000,
    ) -> None:
        if not 1 <= max_nodes <= 128 or not 1 <= max_steps <= 50000:
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid dependency search limits")
        self.sources = sources
        self.identities = identities
        self.priorities = priorities or SourcePriorities()
        self.max_nodes = max_nodes
        self.max_steps = max_steps

    async def resolve(
        self,
        game_id: str,
        roots: Sequence[ResolvedMod],
        *,
        game_version: str | None = None,
        loader: str | None = None,
        optional: frozenset[SourceRef] = frozenset(),
        cancelled: Callable[[], bool] | None = None,
    ) -> DependencyResolution:
        if not roots or len(roots) > self.max_nodes:
            raise ModMeshError(ErrorCode.VALIDATION, "Choose a bounded set of root mods")
        selected: dict[SourceRef, ResolvedMod] = {}
        for root in roots:
            key = root.project.reference
            if root.project.game_id != game_id:
                raise ModMeshError(ErrorCode.VALIDATION, "Root mod belongs to another game")
            if key in selected and selected[key] != root:
                raise ModMeshError(ErrorCode.CONFLICT, "Root project has multiple pinned files")
            selected[key] = root
        projects = {mod.project.reference: mod.project for mod in roots}
        catalogs: dict[SourceRef, tuple[ModVersion, ...]] = {}
        pinned: dict[tuple[str, str], ModVersion] = {}
        failures: list[ResolutionIssue] = []
        best = selected.copy()
        best_edges: tuple[DependencyEdge, ...] = ()
        steps = 0

        def requirements(mod: ResolvedMod) -> list[tuple[SourceRef, Dependency]]:
            result = []
            for declaration in mod.version.dependencies:
                target = declaration.target
                chosen_optional = (
                    target.source_id is not None
                    and target.project_id is not None
                    and SourceRef(target.source_id, target.project_id) in optional
                )
                if declaration.kind == DependencyKind.REQUIRED or (
                    declaration.kind == DependencyKind.OPTIONAL and chosen_optional
                ):
                    result.append((mod.project.reference, declaration))
            return result

        async def options(declaration: Dependency) -> tuple[ModVersion, ...]:
            target = declaration.target
            references: tuple[SourceRef, ...] = ()
            exact: ModVersion | None = None
            if target.source_id and target.version_id:
                key = target.source_id, target.version_id
                if key not in pinned:
                    pinned[key] = await self.sources.get(target.source_id).version(
                        game_id, target.version_id
                    )
                exact = pinned[key]
                if target.project_id and exact.project.project_id != target.project_id:
                    raise ModMeshError(
                        ErrorCode.INTEGRITY, "Dependency version has another project"
                    )
                references = (exact.project,)
            elif target.source_id and target.project_id:
                references = (SourceRef(target.source_id, target.project_id),)
            elif target.canonical_id and self.identities is not None:
                references = self.identities.projects(target.canonical_id)
            if not references:
                raise ModMeshError(ErrorCode.UNSUPPORTED, "Dependency has no resolvable source ID")
            candidates: list[ModVersion] = []
            last_error: ModMeshError | None = None
            for reference in sorted(
                references, key=lambda ref: self.priorities.rank(game_id, ref.source_id)
            ):
                try:
                    source = self.sources.get(reference.source_id)
                    if reference not in projects:
                        project = await source.project(game_id, reference.project_id)
                        if project.reference != reference or project.game_id != game_id:
                            raise ModMeshError(
                                ErrorCode.INTEGRITY, "Dependency project ID mismatch"
                            )
                        projects[reference] = project
                    versions: tuple[ModVersion, ...]
                    if exact is not None:
                        versions = (exact,)
                    else:
                        if reference not in catalogs:
                            values = await source.versions(
                                game_id,
                                reference.project_id,
                                query=VersionQuery(game_version, loader)
                                if source.version_filters
                                else None,
                            )
                            if len(values) > 2000:
                                raise ModMeshError(
                                    ErrorCode.UNSUPPORTED, "Dependency catalog exceeds search limit"
                                )
                            catalogs[reference] = values
                        versions = catalogs[reference]
                    for version in versions:
                        if version.project != reference:
                            raise ModMeshError(
                                ErrorCode.INTEGRITY, "Dependency catalog ID mismatch"
                            )
                        if compatible(version, game_version, loader) and matches_constraint(
                            version, declaration.constraint
                        ):
                            try:
                                primary_file(version)
                            except ModMeshError:
                                continue
                            candidates.append(version)
                except ModMeshError as exc:
                    last_error = exc
            if not candidates and last_error is not None:
                raise last_error
            return tuple(candidates)

        async def solve(
            current: dict[SourceRef, ResolvedMod],
            pending: list[tuple[SourceRef, Dependency]],
            edges: tuple[DependencyEdge, ...],
        ) -> tuple[dict[SourceRef, ResolvedMod], tuple[DependencyEdge, ...]] | None:
            nonlocal steps, best, best_edges
            steps += 1
            if cancelled is not None and cancelled():
                raise ModMeshError(ErrorCode.CANCELLED, "Dependency resolution cancelled")
            if steps > self.max_steps or len(current) > self.max_nodes or len(edges) >= 512:
                raise ModMeshError(ErrorCode.UNSUPPORTED, "Dependency graph search limit reached")
            if len(current) > len(best):
                best, best_edges = current.copy(), edges
            if not pending:
                return current, edges
            parent, declaration = pending[0]
            remaining = pending[1:]
            if declaration.game_versions and game_version not in declaration.game_versions:
                failures.append(
                    ResolutionIssue(
                        "dependency_context",
                        "Dependency game-version condition is not satisfied",
                        parent,
                        declaration,
                    )
                )
                return None
            if declaration.loaders and loader not in declaration.loaders:
                failures.append(
                    ResolutionIssue(
                        "dependency_context",
                        "Dependency loader condition is not satisfied",
                        parent,
                        declaration,
                    )
                )
                return None
            try:
                versions = await options(declaration)
            except ModMeshError as exc:
                if exc.code == ErrorCode.CANCELLED:
                    raise
                failures.append(ResolutionIssue(exc.code.value, exc.message, parent, declaration))
                return None
            for version in versions:
                reference = version.project
                existing = current.get(reference)
                if existing is not None and existing.version.version_id != version.version_id:
                    continue
                edge = DependencyEdge(parent, reference, declaration)
                added_edges = edges if edge in edges else (*edges, edge)
                updated = dict(current)
                added = []
                if existing is None:
                    mod = ResolvedMod(projects[reference], version, primary_file(version))
                    updated[reference] = mod
                    added = requirements(mod)
                result = await solve(updated, remaining + added, added_edges)
                if result is not None:
                    return result
            failures.append(
                ResolutionIssue(
                    "unsatisfied_dependency",
                    "No dependency version satisfies all selected requirements",
                    parent,
                    declaration,
                )
            )
            return None

        pending = [item for root in roots for item in requirements(root)]
        result = await solve(selected, pending, ())
        if result is None:
            return DependencyResolution(
                game_id,
                game_version,
                loader,
                tuple(best.values()),
                best_edges,
                tuple(failures[-20:]),
            )
        mods, edges = result
        return DependencyResolution(game_id, game_version, loader, tuple(mods.values()), edges)
