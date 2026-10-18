"""Concurrent source search, retaining per-source pages, limits and failures."""

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.priorities import SourcePriorities
from modmesh.core.sources import (
    SearchPage,
    SearchRequest,
    SourceAdapter,
    SourceCapability,
    SourceRegistry,
)
from modmesh.domain.models import SourceProject, SourceRef


class SearchState(StrEnum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class SourceSearchResult:
    source_id: str
    state: SearchState
    page: SearchPage | None = None
    error: dict[str, object] | None = None
    handoff_url: str | None = None


@dataclass(frozen=True)
class UnifiedSearchResult:
    request: SearchRequest
    projects: tuple[SourceProject, ...]
    sources: tuple[SourceSearchResult, ...]
    next_offsets: dict[str, int]


class UnifiedSearch:
    def __init__(
        self,
        registry: SourceRegistry,
        *,
        concurrency: int = 4,
        timeout: float = 35.0,
        priorities: SourcePriorities | None = None,
    ) -> None:
        if not 1 <= concurrency <= 16 or not math.isfinite(timeout) or timeout <= 0:
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid search concurrency or timeout")
        self.registry = registry
        self.concurrency = concurrency
        self.timeout = timeout
        self.priorities = priorities or SourcePriorities()

    async def search(
        self,
        request: SearchRequest,
        *,
        source_ids: Sequence[str] | None = None,
        offsets: Mapping[str, int] | None = None,
    ) -> UnifiedSearchResult:
        sources = (
            self.registry.all(request.game_id)
            if source_ids is None
            else tuple(self.registry.get(key) for key in dict.fromkeys(source_ids))
        )
        if not sources:
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "No configured sources support the requested game"
            )
        sources = tuple(
            sorted(
                sources,
                key=lambda source: self.priorities.rank(
                    request.game_id, source.descriptor.source_id
                ),
            )
        )
        offsets = dict(offsets or {})
        if set(offsets) - {source.descriptor.source_id for source in sources} or any(
            value < 0 for value in offsets.values()
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid per-source search offsets")
        semaphore = asyncio.Semaphore(self.concurrency)

        async def search_source(source: SourceAdapter) -> SourceSearchResult:
            key = source.descriptor.source_id
            try:
                status = source.status(request.game_id)
                handoff = source.catalog_url(request.game_id)
            except Exception:
                logging.getLogger("modmesh.search").exception(
                    "Source status failed", extra={"context": {"source_id": key}}
                )
                error = ModMeshError(
                    ErrorCode.NETWORK,
                    "Source availability check failed",
                    details={"source_id": key},
                )
                return SourceSearchResult(key, SearchState.ERROR, error=error.as_dict())
            if not status.available or SourceCapability.SEARCH not in status.capabilities:
                error = ModMeshError(
                    ErrorCode.UNSUPPORTED,
                    status.reason or "Source search is unavailable",
                    details={"source_id": key},
                )
                return SourceSearchResult(
                    key, SearchState.UNAVAILABLE, error=error.as_dict(), handoff_url=handoff
                )
            async with semaphore:
                try:
                    async with asyncio.timeout(self.timeout):
                        page = await source.search(
                            replace(request, offset=offsets.get(key, request.offset))
                        )
                    if any(
                        project.game_id != request.game_id or project.reference.source_id != key
                        for project in page.projects
                    ):
                        raise ModMeshError(
                            ErrorCode.NETWORK,
                            "Source search returned mismatched game or source IDs",
                        )
                    return SourceSearchResult(
                        key, SearchState.SUCCESS, page, handoff_url=page.handoff_url or handoff
                    )
                except TimeoutError:
                    error = ModMeshError(
                        ErrorCode.NETWORK, "Source search timed out", details={"source_id": key}
                    )
                except ModMeshError as exc:
                    if exc.code in {ErrorCode.UNSUPPORTED, ErrorCode.AUTHENTICATION}:
                        return SourceSearchResult(
                            key, SearchState.UNAVAILABLE, error=exc.as_dict(), handoff_url=handoff
                        )
                    error = exc
                except Exception:
                    logging.getLogger("modmesh.search").exception(
                        "Source search failed", extra={"context": {"source_id": key}}
                    )
                    error = ModMeshError(
                        ErrorCode.NETWORK,
                        "Source search failed unexpectedly",
                        details={"source_id": key},
                    )
                return SourceSearchResult(
                    key, SearchState.ERROR, error=error.as_dict(), handoff_url=handoff
                )

        tasks = [asyncio.create_task(search_source(source)) for source in sources]
        try:
            results = tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        projects: list[SourceProject] = []
        seen: set[SourceRef] = set()
        next_offsets: dict[str, int] = {}
        for result in results:
            if result.page is None:
                continue
            for project in result.page.projects:
                if project.reference not in seen:
                    seen.add(project.reference)
                    projects.append(project)
            if result.page.next_offset is not None:
                next_offsets[result.source_id] = result.page.next_offset
        return UnifiedSearchResult(request, tuple(projects), results, next_offsets)
