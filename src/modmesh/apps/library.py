from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.discovery import DiscoveryEngine, DiscoveryResult
from modmesh.core.games import DiscoveryContext, GameRegistry
from modmesh.domain.models import GameInstallation
from modmesh.domain.workspaces import installation_identity
from modmesh.storage.metadata import MetadataStore


class GameLibrary:
    def __init__(self, games: GameRegistry, database: Path) -> None:
        self.games = games
        self.database = database

    def inspect(self, game_id: str, path: Path, context: DiscoveryContext) -> GameInstallation:
        adapter = self.games.get(game_id)
        installation = adapter.inspect(path.expanduser().resolve(), context.platform)
        if installation is None or not adapter.validate(installation).valid:
            raise ModMeshError(ErrorCode.VALIDATION, "Path is not a valid game installation")
        return replace(
            installation,
            installation_id=installation_identity(game_id, installation.path, context.platform),
        )

    def discover(
        self,
        context: DiscoveryContext,
        *,
        game_id: str | None = None,
        manual: tuple[str, Path] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DiscoveryResult:
        if cancelled is not None and cancelled():
            raise ModMeshError(ErrorCode.CANCELLED, "Game discovery cancelled")
        with MetadataStore(self.database) as store:
            engine = DiscoveryEngine(self.games, store)
            if manual is not None:
                engine.register_manual(*manual, context)
            return engine.discover(context, game_id=game_id, cancelled=cancelled)
