"""Asynchronous source contracts with explicit policy and credential capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.dependencies import Dependency
from modmesh.domain.models import ModFile, ModVersion, SourceProject
from modmesh.domain.validation import require_id, require_text, validate_web_url


class SourceCapability(StrEnum):
    SEARCH = "search"
    PROJECT = "project"
    VERSIONS = "versions"
    FILES = "files"
    DEPENDENCIES = "dependencies"
    HASHES = "hashes"
    DOWNLOAD_DIRECT = "download_direct"
    EXTERNAL_HANDOFF = "external_handoff"


class SearchSort(StrEnum):
    RELEVANCE = "relevance"
    DOWNLOADS = "downloads"
    UPDATED = "updated"


class SearchScope(StrEnum):
    CATALOG = "catalog"
    TRENDING = "trending"


@dataclass(frozen=True)
class VersionQuery:
    game_version: str | None = None
    loader: str | None = None

    def __post_init__(self) -> None:
        for key in ("game_version", "loader"):
            if (value := getattr(self, key)) is not None:
                require_text(value, key)


@dataclass(frozen=True)
class SourceDescriptor:
    source_id: str
    name: str
    games: frozenset[str]
    capabilities: frozenset[SourceCapability]

    def __post_init__(self) -> None:
        require_id(self.source_id, "source ID")
        require_text(self.name, "source name")
        for game in self.games:
            require_id(game, "game ID")


@dataclass(frozen=True)
class SourceStatus:
    available: bool
    capabilities: frozenset[SourceCapability]
    reason: str | None = None


@dataclass(frozen=True)
class SearchRequest:
    game_id: str
    query: str
    game_version: str | None = None
    loader: str | None = None
    offset: int = 0
    limit: int = 20
    sort: SearchSort = SearchSort.RELEVANCE

    def __post_init__(self) -> None:
        require_id(self.game_id, "game ID")
        if len(self.query) > 256 or "\x00" in self.query:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid search query")
        if self.offset < 0 or not 1 <= self.limit <= 100:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid search pagination")
        for key in ("game_version", "loader"):
            if (value := getattr(self, key)) is not None:
                require_text(value, key)


@dataclass(frozen=True)
class SearchPage:
    projects: tuple[SourceProject, ...]
    total: int | None = None
    next_offset: int | None = None
    warnings: tuple[str, ...] = ()
    handoff_url: str | None = None
    scope: SearchScope = SearchScope.CATALOG


class DownloadMode(StrEnum):
    DIRECT = "direct"
    EXTERNAL = "external"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DownloadAccess:
    mode: DownloadMode
    url: str | None = field(default=None, repr=False)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.mode != DownloadMode.UNAVAILABLE and self.url is None:
            raise ModMeshError(ErrorCode.VALIDATION, "Download access requires an explicit URL")
        validate_web_url(self.url)


class SourceAdapter(ABC):
    @property
    @abstractmethod
    def descriptor(self) -> SourceDescriptor:
        """Static source metadata. Actual access is described by status()."""

    def status(self, game_id: str) -> SourceStatus:
        if game_id not in self.descriptor.games:
            return SourceStatus(
                False, frozenset(), "This source does not support the selected game"
            )
        return SourceStatus(True, self.descriptor.capabilities)

    def require(self, game_id: str, capability: SourceCapability) -> None:
        status = self.status(game_id)
        if not status.available or capability not in status.capabilities:
            raise ModMeshError(
                ErrorCode.UNSUPPORTED,
                status.reason or "Source capability is unavailable",
                details={"source_id": self.descriptor.source_id, "capability": capability.value},
            )

    def catalog_url(self, game_id: str) -> str | None:
        return None

    async def search(self, request: SearchRequest) -> SearchPage:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Search is not implemented for this source")

    async def project(self, game_id: str, project_id: str) -> SourceProject:
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Project lookup is not implemented for this source"
        )

    async def versions(
        self, game_id: str, project_id: str, *, query: VersionQuery | None = None
    ) -> tuple[ModVersion, ...]:
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Version lookup is not implemented for this source"
        )

    async def files(self, game_id: str, project_id: str) -> tuple[ModFile, ...]:
        self.require(game_id, SourceCapability.FILES)
        return tuple(
            file for version in await self.versions(game_id, project_id) for file in version.files
        )

    async def dependencies(
        self, game_id: str, project_id: str, version_id: str
    ) -> tuple[Dependency, ...]:
        self.require(game_id, SourceCapability.DEPENDENCIES)
        for version in await self.versions(game_id, project_id):
            if version.version_id == version_id:
                if not version.dependencies_known:
                    raise ModMeshError(
                        ErrorCode.UNSUPPORTED, "Dependency metadata is unavailable for this version"
                    )
                return version.dependencies
        raise ModMeshError(ErrorCode.NOT_FOUND, "Source version was not found")

    async def resolve_download(self, game_id: str, file: ModFile) -> DownloadAccess:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Downloads are not implemented for this source")

    async def aclose(self) -> None:
        """Release resources owned by this adapter."""
        return None


class UnavailableSource(SourceAdapter):
    def __init__(
        self, descriptor: SourceDescriptor, reason: str, website: str | None = None
    ) -> None:
        self._descriptor = descriptor
        self._reason = reason
        self._website = website

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    def status(self, game_id: str) -> SourceStatus:
        base = super().status(game_id)
        return SourceStatus(False, frozenset(), self._reason) if base.available else base

    def catalog_url(self, game_id: str) -> str | None:
        return self._website if game_id in self.descriptor.games else None


class SourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        key = adapter.descriptor.source_id
        if key in self._adapters:
            raise ModMeshError(
                ErrorCode.CONFLICT,
                "Source adapter is already registered",
                details={"source_id": key},
            )
        self._adapters[key] = adapter

    def get(self, source_id: str) -> SourceAdapter:
        try:
            return self._adapters[source_id]
        except KeyError as exc:
            raise ModMeshError(
                ErrorCode.NOT_FOUND,
                "Source adapter is not registered",
                details={"source_id": source_id},
            ) from exc

    def all(self, game_id: str | None = None) -> tuple[SourceAdapter, ...]:
        return tuple(
            adapter
            for _, adapter in sorted(self._adapters.items())
            if game_id is None or game_id in adapter.descriptor.games
        )

    async def aclose(self) -> None:
        for adapter in self.all():
            await adapter.aclose()
