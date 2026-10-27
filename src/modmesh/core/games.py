from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from modmesh.core.capabilities import CapabilityEvidence, CapabilitySupport, SupportState
from modmesh.core.capabilities import GameCapability as GameCapability
from modmesh.core.diagnostics import Diagnostic, ErrorCode, ModMeshError
from modmesh.domain.models import Game, GameInstallation, InstalledMod, ModFile, Platform
from modmesh.domain.validation import relative_path, require_id, require_text


@dataclass(frozen=True)
class DiscoveryContext:
    platform: Platform
    roots: tuple[Path, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    home: Path = field(default_factory=Path.home)

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class EnvironmentIssue:
    code: str
    message: str
    blocking: bool = False

    @property
    def diagnostic(self) -> Diagnostic:
        return Diagnostic.environment(self.code, self.message, self.blocking)


@dataclass(frozen=True)
class EnvironmentReport:
    issues: tuple[EnvironmentIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(issue.blocking for issue in self.issues)


@dataclass(frozen=True)
class ModLocation:
    name: str
    path: Path
    layout: str


@dataclass(frozen=True)
class InstallRule:
    rule_id: str
    destination: str
    layout: str
    extensions: frozenset[str]
    ignored_files: frozenset[str] = frozenset()
    mappings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_id(self.rule_id, "install rule ID")
        require_text(self.layout, "install layout")
        object.__setattr__(self, "destination", relative_path(self.destination))
        if self.mappings and self.layout != "archive":
            raise ModMeshError(ErrorCode.VALIDATION, "Prefix mappings require an archive")
        for source, target in self.mappings:
            relative_path(source)
            if target:
                relative_path(target)
        if not self.extensions or any(
            ext != "*" and not ext.startswith(".") for ext in self.extensions
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Installation rules require explicit file extensions"
            )


class GameAdapter(ABC):
    def support(
        self, installation: GameInstallation | None = None
    ) -> tuple[CapabilitySupport, ...]:
        environment = (
            self.validate(installation) if installation is not None else EnvironmentReport()
        )
        result = []
        for capability in GameCapability:
            state = SupportState.AVAILABLE
            message = "기존 어댑터의 기능을 사용합니다. 실게임 모드 로딩은 미검증입니다."
            prerequisites: tuple[str, ...] = ()
            if capability not in self.capabilities:
                state, message = SupportState.UNSUPPORTED, "제공하지 않는 기능입니다."
            elif installation is not None and not environment.valid:
                state, message = SupportState.BLOCKED, "게임 환경 검사를 먼저 해결하세요."
                prerequisites = tuple(
                    issue.message for issue in environment.issues if issue.blocking
                )
            elif capability == GameCapability.WORKSHOP_HANDOFF:
                state, message = (
                    SupportState.HANDOFF,
                    "구독과 파일 관리는 공식 Steam에서 진행하세요.",
                )
            elif capability == GameCapability.VERSION and (
                installation is None
                or (installation.version.value is None and installation.version.build_id is None)
            ):
                state, message = SupportState.UNVERIFIED, "현재 게임 버전의 근거가 부족합니다."
            result.append(
                CapabilitySupport(
                    capability,
                    state,
                    message,
                    prerequisites,
                    (
                        CapabilityEvidence("implementation", type(self).__name__, True),
                        CapabilityEvidence("game_loading", "실제 게임에서 모드 로딩 미검증"),
                    ),
                )
            )
        return tuple(result)

    @property
    @abstractmethod
    def game(self) -> Game:
        pass

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[GameCapability]:
        pass

    @abstractmethod
    def candidates(self, context: DiscoveryContext) -> Iterable[Path]:
        pass

    @abstractmethod
    def inspect(self, path: Path, platform: Platform) -> GameInstallation | None:
        pass

    def validate(self, installation: GameInstallation) -> EnvironmentReport:
        issues: list[EnvironmentIssue] = []
        if (
            installation.game_id != self.game.game_id
            or installation.edition not in self.game.editions
        ):
            issues.append(
                EnvironmentIssue("wrong_game", "Installation does not match this adapter", True)
            )
        if installation.platform not in self.game.platforms:
            issues.append(
                EnvironmentIssue("unsupported_platform", "Unsupported game platform", True)
            )
        if not installation.path.is_dir():
            issues.append(EnvironmentIssue("missing_path", "Game directory does not exist", True))
        if installation.version.value is None:
            issues.append(EnvironmentIssue("unknown_version", "Game version is not verified"))
        return EnvironmentReport(tuple(issues))

    def validate_operation(self, installation: GameInstallation) -> EnvironmentReport:
        return EnvironmentReport()

    def validate_artifacts(
        self, installation: GameInstallation, files: tuple[tuple[ModFile, Path], ...]
    ) -> EnvironmentReport:
        return EnvironmentReport()

    def validate_removal(
        self,
        installation: GameInstallation,
        paths: tuple[str, ...],
        restored: tuple[Path, ...] = (),
    ) -> EnvironmentReport:
        return EnvironmentReport()

    def profile_settings(self, installation: GameInstallation) -> tuple[str, ...]:
        return ()

    def validate_profile_payload(
        self,
        installation: GameInstallation,
        files: tuple[tuple[ModFile, Path], ...],
        replaced: frozenset[str],
    ) -> EnvironmentReport:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Profile payload validation is unavailable")

    def validate_profile(
        self,
        installation: GameInstallation,
        files: tuple[tuple[str, Path], ...],
        replaced: tuple[str, ...],
    ) -> EnvironmentReport:
        return EnvironmentReport()

    def mod_locations(self, installation: GameInstallation) -> tuple[ModLocation, ...]:
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Mod locations are not implemented for this adapter"
        )

    def scan_mods(self, installation: GameInstallation) -> tuple[InstalledMod, ...]:
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Mod discovery is not implemented for this adapter"
        )

    def installation_rules(
        self, installation: GameInstallation, file: ModFile
    ) -> tuple[InstallRule, ...]:
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Installation rules are not implemented for this adapter"
        )

    def validate_payload(
        self, installation: GameInstallation, rule: InstallRule, paths: tuple[str, ...]
    ) -> EnvironmentReport:
        return EnvironmentReport()

    def auxiliary_files(
        self, installation: GameInstallation, rule: InstallRule, paths: tuple[str, ...]
    ) -> Mapping[str, bytes]:
        return {}

    def removal_rules(self, installation: GameInstallation) -> tuple[InstallRule, ...]:
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Removal rules are not implemented for this adapter"
        )


class GameRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, GameAdapter] = {}
        self.issues: tuple[EnvironmentIssue, ...] = ()

    def replace(self, adapter: GameAdapter, *, expected: GameAdapter) -> None:
        key = adapter.game.game_id
        if self._adapters.get(key) is not expected:
            raise ModMeshError(ErrorCode.CONFLICT, "Game registration changed; refresh it first")
        self._adapters[key] = adapter

    def register(self, adapter: GameAdapter) -> None:
        key = adapter.game.game_id
        if key in self._adapters:
            raise ModMeshError(
                ErrorCode.CONFLICT, "A game adapter is already registered", details={"game_id": key}
            )
        self._adapters[key] = adapter

    def get(self, game_id: str) -> GameAdapter:
        require_id(game_id, "game ID")
        try:
            return self._adapters[game_id]
        except KeyError as exc:
            raise ModMeshError(
                ErrorCode.NOT_FOUND, "Game adapter is not registered", details={"game_id": game_id}
            ) from exc

    def all(self, platform: Platform | None = None) -> tuple[GameAdapter, ...]:
        return tuple(
            adapter
            for _, adapter in sorted(self._adapters.items())
            if platform is None or platform in adapter.game.platforms
        )
