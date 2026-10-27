from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import InstallStrategyKind, InstallStrategyRule, ModRootRule
from modmesh.core.games import InstallRule
from modmesh.domain.models import ModFile


class InstallationStrategy(Protocol):
    @property
    def kind(self) -> InstallStrategyKind: ...

    def select(
        self,
        specification: InstallStrategyRule,
        root: ModRootRule,
        file: ModFile,
        loader_present: bool,
    ) -> tuple[InstallRule, ...]: ...


@dataclass(frozen=True)
class FilePlacement:
    kind: InstallStrategyKind

    def select(
        self,
        specification: InstallStrategyRule,
        root: ModRootRule,
        file: ModFile,
        loader_present: bool,
    ) -> tuple[InstallRule, ...]:
        if self.kind == InstallStrategyKind.READONLY:
            return ()
        if self.kind == InstallStrategyKind.PLUGIN and not loader_present:
            return ()
        suffix = PurePosixPath(file.filename).suffix.lower()
        archive = suffix == ".zip" and self.kind != InstallStrategyKind.FILE
        if self.kind in {InstallStrategyKind.ARCHIVE, InstallStrategyKind.MAPPED} and not archive:
            return ()
        if not archive and suffix not in specification.extensions:
            return ()
        docs = frozenset({"readme.md", "readme.txt", "license", "license.txt", "changelog.txt"})
        return (
            InstallRule(
                specification.strategy_id,
                root.path,
                "archive" if archive else "file",
                frozenset(specification.extensions),
                docs if archive else frozenset(),
                tuple((item.source_prefix, item.target_prefix) for item in specification.mappings),
            ),
        )


class InstallationStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[InstallStrategyKind, InstallationStrategy] = {}

    def register(self, strategy: InstallationStrategy) -> None:
        if strategy.kind in self._strategies:
            raise ModMeshError(ErrorCode.CONFLICT, "Installation strategy already registered")
        self._strategies[strategy.kind] = strategy

    def select(
        self,
        specification: InstallStrategyRule,
        root: ModRootRule,
        file: ModFile,
        *,
        loader_present: bool,
    ) -> tuple[InstallRule, ...]:
        try:
            strategy = self._strategies[specification.kind]
        except KeyError as exc:
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "Installation strategy is unavailable"
            ) from exc
        return strategy.select(specification, root, file, loader_present)

    @classmethod
    def standard(cls) -> InstallationStrategyRegistry:
        registry = cls()
        for kind in InstallStrategyKind:
            registry.register(FilePlacement(kind))
        return registry
