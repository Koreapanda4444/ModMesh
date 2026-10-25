import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from modmesh.adapters.games.ark_evolved import ArkEvolvedAdapter
from modmesh.adapters.games.minecraft import MinecraftAdapter
from modmesh.adapters.games.palworld import PalworldAdapter
from modmesh.adapters.sources.curseforge import CurseForgeSource
from modmesh.adapters.sources.custom import CustomSource
from modmesh.adapters.sources.modrinth import ModrinthSource
from modmesh.adapters.sources.nexus import NexusSource
from modmesh.adapters.sources.workshop import WorkshopSource
from modmesh.core.config import load_client_settings
from modmesh.core.diagnostics import ModMeshError
from modmesh.core.games import GameRegistry
from modmesh.core.network import EnvironmentCredentials
from modmesh.core.priorities import SourcePriorities
from modmesh.core.sources import SourceAdapter, SourceDescriptor, SourceRegistry, UnavailableSource

DEFAULT_SOURCE_PRIORITIES = {
    "ark-evolved": ("workshop",),
    "minecraft": ("modrinth", "curseforge", "nexus", "custom"),
    "palworld": ("nexus", "custom"),
}

DOWNLOAD_DOMAINS = {
    "modrinth": ("cdn.modrinth.com",),
    "nexus": ("nexusmods.com", "nexus-cdn.com"),
    "curseforge": ("forgecdn.net", "cursecdn.com"),
}


def source_priorities(overrides: Mapping[str, tuple[str, ...]]) -> SourcePriorities:
    return SourcePriorities({**DEFAULT_SOURCE_PRIORITIES, **overrides})


def download_domains(registry: SourceRegistry) -> dict[str, tuple[str, ...]]:
    return {
        **DOWNLOAD_DOMAINS,
        **{
            source.descriptor.source_id: source.download_hosts()
            for source in registry.all()
            if source.download_hosts()
        },
    }


def create_game_registry() -> GameRegistry:
    registry = GameRegistry()
    registry.register(PalworldAdapter())
    registry.register(MinecraftAdapter())
    registry.register(ArkEvolvedAdapter())
    return registry


@asynccontextmanager
async def source_registry(
    environment: Mapping[str, str] | None = None,
    *,
    database: Path | None = None,
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
        registry.register(WorkshopSource())
        registry.register(
            CustomSource(database or load_client_settings(env=environment).paths.database)
        )
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
