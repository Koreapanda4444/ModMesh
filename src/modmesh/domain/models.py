"""Normalized game/mod models. Display names never establish identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import Dependency
from modmesh.domain.validation import relative_path, require_id, require_text, validate_web_url


class Platform(StrEnum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


class ReleaseChannel(StrEnum):
    RELEASE = "release"
    BETA = "beta"
    ALPHA = "alpha"


@dataclass(frozen=True)
class Game:
    game_id: str
    name: str
    editions: tuple[str, ...]
    platforms: frozenset[Platform]

    def __post_init__(self) -> None:
        require_id(self.game_id, "game ID")
        require_text(self.name, "game name")
        if not self.editions or not self.platforms:
            raise ModMeshError(ErrorCode.VALIDATION, "Game needs supported editions and platforms")
        for edition in self.editions:
            require_text(edition, "edition")


@dataclass(frozen=True)
class GameVersion:
    value: str | None = None
    build_id: str | None = None
    evidence: str | None = None

    def __post_init__(self) -> None:
        for key in ("value", "build_id", "evidence"):
            if (value := getattr(self, key)) is not None:
                require_text(value, key)


@dataclass(frozen=True)
class Loader:
    name: str
    version: str | None = None

    def __post_init__(self) -> None:
        require_id(self.name, "loader")
        if self.version is not None:
            require_text(self.version, "loader version")


@dataclass(frozen=True)
class GameInstallation:
    game_id: str
    path: Path
    edition: str
    platform: Platform
    version: GameVersion = field(default_factory=GameVersion)
    loader: Loader | None = None
    installation_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        require_id(self.game_id, "game ID")
        require_text(self.edition, "edition")
        require_text(self.installation_id, "installation ID")
        if not self.path.is_absolute():
            raise ModMeshError(ErrorCode.VALIDATION, "Installation path must be absolute")


@dataclass(frozen=True, order=True)
class SourceRef:
    source_id: str
    project_id: str

    def __post_init__(self) -> None:
        require_id(self.source_id, "source ID")
        require_text(self.project_id, "source project ID")


@dataclass(frozen=True)
class SourceProject:
    reference: SourceRef
    game_id: str
    name: str
    summary: str = ""
    authors: tuple[str, ...] = ()
    web_url: str | None = None

    def __post_init__(self) -> None:
        require_id(self.game_id, "game ID")
        require_text(self.name, "project name")
        validate_web_url(self.web_url)


@dataclass(frozen=True)
class Mod:
    mod_id: str
    game_id: str
    name: str
    projects: tuple[SourceRef, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.mod_id, "mod ID")
        require_id(self.game_id, "game ID")
        require_text(self.name, "mod name")
        if len(set(self.projects)) != len(self.projects):
            raise ModMeshError(ErrorCode.VALIDATION, "Duplicate source project reference")


@dataclass(frozen=True)
class ModFile:
    project: SourceRef
    file_id: str
    filename: str
    size: int | None = None
    download_url: str | None = field(default=None, repr=False)
    hashes: tuple[FileHash, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.file_id, "source file ID")
        require_text(self.filename, "filename")
        if (
            relative_path(self.filename) != self.filename
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Mod filename must be a basename")
        if self.size is not None and (isinstance(self.size, bool) or self.size < 0):
            raise ModMeshError(ErrorCode.VALIDATION, "File size cannot be negative")
        validate_web_url(self.download_url)
        if len({value.algorithm for value in self.hashes}) != len(self.hashes):
            raise ModMeshError(ErrorCode.VALIDATION, "Duplicate file hash algorithm")


@dataclass(frozen=True)
class ModVersion:
    project: SourceRef
    version_id: str
    number: str
    game_versions: frozenset[str] = frozenset()
    loaders: frozenset[str] = frozenset()
    files: tuple[ModFile, ...] = ()
    channel: ReleaseChannel = ReleaseChannel.RELEASE
    dependencies: tuple[Dependency, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.version_id, "source version ID")
        require_text(self.number, "mod version")
        if any(file.project != self.project for file in self.files):
            raise ModMeshError(ErrorCode.VALIDATION, "Version file belongs to another project")
        if len({file.file_id for file in self.files}) != len(self.files):
            raise ModMeshError(ErrorCode.VALIDATION, "Duplicate file ID within a version")


@dataclass(frozen=True)
class InstalledFile:
    path: str
    managed: bool
    sha256: FileHash | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", relative_path(self.path))
        if self.sha256 is not None and self.sha256.algorithm != HashAlgorithm.SHA256:
            raise ModMeshError(ErrorCode.VALIDATION, "Installed file identity requires SHA-256")


@dataclass(frozen=True)
class InstalledMod:
    installation_id: str
    mod_id: str
    version: str | None
    files: tuple[InstalledFile, ...]
    source: SourceRef | None = None

    def __post_init__(self) -> None:
        require_text(self.installation_id, "installation ID")
        require_text(self.mod_id, "mod ID")
        if self.version is not None:
            require_text(self.version, "installed mod version")
        if len({file.path.casefold() for file in self.files}) != len(self.files):
            raise ModMeshError(ErrorCode.VALIDATION, "Duplicate installed file path")
