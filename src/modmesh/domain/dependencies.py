from dataclasses import dataclass, field
from enum import StrEnum

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.validation import relative_path, require_id, require_text


class DependencyKind(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    INCOMPATIBLE = "incompatible"
    EMBEDDED = "embedded"
    TOOL = "tool"
    INCLUDE = "include"


@dataclass(frozen=True)
class DependencyTarget:
    source_id: str | None = None
    project_id: str | None = None
    version_id: str | None = None
    canonical_id: str | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        if self.source_id is not None:
            require_id(self.source_id, "dependency source ID")
        if (self.project_id is not None or self.version_id is not None) and self.source_id is None:
            raise ModMeshError(ErrorCode.VALIDATION, "Native dependency IDs require a source")
        for name in ("project_id", "version_id", "canonical_id", "filename"):
            if (value := getattr(self, name)) is not None:
                require_text(value, name)
        if not any((self.project_id, self.version_id, self.canonical_id, self.filename)):
            raise ModMeshError(ErrorCode.VALIDATION, "Dependency must identify a target")
        if self.filename is not None and (
            relative_path(self.filename) != self.filename
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Embedded dependency filename must be a basename"
            )


@dataclass(frozen=True)
class VersionConstraint:
    exact_versions: frozenset[str] = frozenset()
    expression: str | None = None
    scheme: str | None = None

    def __post_init__(self) -> None:
        for value in self.exact_versions:
            require_text(value, "exact dependency version")
        if self.expression is not None:
            require_text(self.expression, "version constraint")
            if self.scheme is None:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Range expressions require an explicit version scheme"
                )
        if self.scheme is not None:
            require_text(self.scheme, "version scheme")
        if self.exact_versions and self.expression is not None:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Use either exact versions or a range expression"
            )


@dataclass(frozen=True)
class Dependency:
    kind: DependencyKind
    target: DependencyTarget
    constraint: VersionConstraint = field(default_factory=VersionConstraint)
    game_versions: frozenset[str] = frozenset()
    loaders: frozenset[str] = frozenset()

    @property
    def resolvable(self) -> bool:
        return bool(self.target.project_id or self.target.version_id or self.target.canonical_id)
