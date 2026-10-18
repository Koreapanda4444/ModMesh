"""Composition root. Concrete adapters are registered as their milestones land."""

from modmesh.core.games import GameRegistry


def create_game_registry() -> GameRegistry:
    return GameRegistry()
