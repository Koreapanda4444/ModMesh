from pathlib import Path
from typing import Protocol

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import LoaderKind, LoaderRule
from modmesh.core.games import EnvironmentReport
from modmesh.domain.models import GameInstallation, Loader, ModFile


class ArtifactInspector(Protocol):
    def inspect(
        self, installation: GameInstallation, files: tuple[tuple[ModFile, Path], ...]
    ) -> EnvironmentReport: ...


class LoaderStrategy(ArtifactInspector, Protocol):
    @property
    def kind(self) -> LoaderKind: ...

    def detect(self, root: Path, definition: LoaderRule) -> Loader | None: ...

    def payload(self, paths: tuple[str, ...]) -> EnvironmentReport: ...

    def auxiliary(self, paths: tuple[str, ...]) -> dict[str, bytes]: ...


class LoaderStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[LoaderKind, LoaderStrategy] = {}

    def register(self, strategy: LoaderStrategy) -> None:
        if strategy.kind in self._strategies:
            raise ModMeshError(ErrorCode.CONFLICT, "Loader strategy already registered")
        self._strategies[strategy.kind] = strategy

    def get(self, kind: LoaderKind) -> LoaderStrategy:
        try:
            return self._strategies[kind]
        except KeyError as exc:
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "Loader requires a legacy game bridge"
            ) from exc
