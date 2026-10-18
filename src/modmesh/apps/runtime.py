"""Composition root. Concrete adapters are registered as their milestones land."""

import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager

from modmesh.adapters.games.palworld import PalworldAdapter
from modmesh.adapters.sources.curseforge import CurseForgeSource
from modmesh.adapters.sources.modrinth import ModrinthSource
from modmesh.adapters.sources.nexus import NexusSource
from modmesh.core.diagnostics import ModMeshError
from modmesh.core.games import GameRegistry
from modmesh.core.network import EnvironmentCredentials
from modmesh.core.priorities import SourcePriorities
from modmesh.core.sources import SourceAdapter, SourceDescriptor, SourceRegistry, UnavailableSource

DEFAULT_SOURCE_PRIORITIES = {
    "minecraft": ("modrinth", "curseforge", "nexus"),
    "palworld": ("nexus",),
}

DOWNLOAD_DOMAINS = {
    "modrinth": ("cdn.modrinth.com",),
    "nexus": ("nexusmods.com", "nexus-cdn.com"),
    "curseforge": ("forgecdn.net", "cursecdn.com"),
}


def source_priorities(overrides: Mapping[str, tuple[str, ...]]) -> SourcePriorities:
    return SourcePriorities({**DEFAULT_SOURCE_PRIORITIES, **overrides})


def create_game_registry() -> GameRegistry:
    registry = GameRegistry()
    registry.register(PalworldAdapter())
    return registry


@asynccontextmanager
async def source_registry(
    environment: Mapping[str, str] | None = None,
) -> AsyncIterator[SourceRegistry]:
    registry = SourceRegistry()
    credentials = EnvironmentCredentials(os.environ if environment is None else environment)

    def add(
        factory: Callable[[], SourceAdapter], descriptor: SourceDescriptor, website: str
    ) -> None:
        try:
            source = factory()
        except ModMeshError as exc:
            source = UnavailableSource(descriptor, exc.message, website)
        registry.register(source)

    try:
        add(
            lambda: NexusSource(credentials.get("nexus")),
            NexusSource.descriptor,
            "https://www.nexusmods.com",
        )
        add(ModrinthSource, ModrinthSource.descriptor, "https://modrinth.com/mods")
        add(
            lambda: CurseForgeSource(credentials.get("curseforge")),
            CurseForgeSource.descriptor,
            "https://www.curseforge.com/minecraft",
        )
        yield registry
    finally:
        await registry.aclose()
