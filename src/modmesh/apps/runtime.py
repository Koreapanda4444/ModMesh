"""Composition root. Concrete adapters are registered as their milestones land."""

from modmesh.adapters.games.palworld import PalworldAdapter
from modmesh.core.games import GameRegistry


def create_game_registry() -> GameRegistry:
    registry = GameRegistry()
    registry.register(PalworldAdapter())
    return registry
