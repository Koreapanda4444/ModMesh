from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from modmesh.adapters.games.steam import read_appmanifest, steam_installations
from modmesh.adapters.loaders import standard_loaders
from modmesh.core.capabilities import CapabilityEvidence, CapabilitySupport, SupportState
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import GameRule, InstallStrategyKind
from modmesh.core.games import (
    DiscoveryContext,
    EnvironmentIssue,
    EnvironmentReport,
    GameAdapter,
    GameCapability,
    InstallRule,
    ModLocation,
)
from modmesh.core.planning import target_path
from modmesh.core.processes import executable_guard
from modmesh.core.rules import RegisteredRule
from modmesh.core.strategies import InstallationStrategyRegistry
from modmesh.domain.models import (
    Game,
    GameInstallation,
    GameVersion,
    InstalledFile,
    InstalledMod,
    ModFile,
    Platform,
)


def rule_candidates(rule: GameRule, context: DiscoveryContext) -> Iterable[Path]:
    yield from context.roots
    if rule.store_ids.steam is not None:
        yield from (item.path for item in steam_installations(context, rule.store_ids.steam))


def rule_files_present(rule: GameRule, root: Path) -> bool:
    try:
        return root.is_dir() and all(
            target_path(root, name).is_file() for name in rule.detect.required_files
        )
    except (OSError, ModMeshError):
        return False


class RuleGameAdapter(GameAdapter):
    def __init__(self, entry: RegisteredRule) -> None:
        self.entry = entry
        self.rule = entry.rule
        self.strategies = InstallationStrategyRegistry.standard()
        self.loader_strategy = standard_loaders().get(self.rule.loader.strategy)
        self._game = Game(
            self.rule.game_id, self.rule.name, (self.rule.edition,), frozenset(self.rule.platforms)
        )

    @property
    def game(self) -> Game:
        return self._game

    @property
    def capabilities(self) -> frozenset[GameCapability]:
        implemented = {
            GameCapability.DISCOVERY,
            GameCapability.VERSION,
            GameCapability.MOD_DISCOVERY,
            GameCapability.INSTALL_RULES,
            GameCapability.REMOVAL_RULES,
            GameCapability.PROFILES,
            GameCapability.WORKSHOP_HANDOFF,
        }
        return (
            frozenset(self.rule.capabilities) & implemented if self.entry.approved else frozenset()
        )

    def candidates(self, context: DiscoveryContext) -> Iterable[Path]:
        yield from rule_candidates(self.rule, context)

    def inspect(self, path: Path, platform: Platform) -> GameInstallation | None:
        if platform not in self.game.platforms:
            return None
        root = path.resolve()
        try:
            if not rule_files_present(self.rule, root):
                return None
            build_id = None
            app_id = self.rule.store_ids.steam
            steam = None
            if app_id is not None:
                manifest = root.parent.parent / f"appmanifest_{app_id}.acf"
                if manifest.is_file() and not manifest.is_symlink() and not manifest.is_junction():
                    try:
                        candidate = read_appmanifest(manifest, app_id)
                        if candidate.path == root:
                            steam = candidate
                    except (OSError, ModMeshError):
                        pass
                if self.rule.detect.steam_manifest_required and steam is None:
                    return None
                if self.rule.game_version.strategy == "steam-build" and steam is not None:
                    build_id = steam.build_id
            evidence = "game-rule:" + self.rule.digest
            if build_id is not None:
                evidence += ":steam-appmanifest:" + str(app_id)
            return GameInstallation(
                self.game.game_id,
                root,
                self.rule.edition,
                platform,
                GameVersion(build_id=build_id, evidence=evidence),
                self.loader_strategy.detect(root, self.rule.loader),
            )
        except (OSError, ModMeshError):
            return None

    def validate(self, installation: GameInstallation) -> EnvironmentReport:
        issues = list(super().validate(installation).issues)
        if self.inspect(installation.path, installation.platform) is None:
            issues.append(
                EnvironmentIssue(
                    "required_files_missing", "Required game files are missing or indirect", True
                )
            )
        for root in self.rule.mod_roots:
            try:
                path = target_path(installation.path, root.path)
                if path.exists() and not path.is_dir():
                    issues.append(
                        EnvironmentIssue("invalid_mod_root", "Mod root is not a directory", True)
                    )
            except (OSError, ModMeshError):
                issues.append(
                    EnvironmentIssue("unsafe_mod_root", "Mod root is indirect or unsafe", True)
                )
        issues.append(
            EnvironmentIssue(
                "game_loading_unverified", "Rule validation does not verify in-game mod loading"
            )
        )
        return EnvironmentReport(tuple(issues))

    def validate_operation(self, installation: GameInstallation) -> EnvironmentReport:
        issues = []
        if not self.entry.approved:
            issues.append(
                EnvironmentIssue(
                    "rule_unapproved", "Approve the game rule before changing files", True
                )
            )
        if self._loader_required and installation.loader is None:
            issues.append(
                EnvironmentIssue(
                    "loader_missing", "Install and verify the required loader separately", True
                )
            )
        executables = [
            name for name in self.rule.detect.required_files if name.lower().endswith(".exe")
        ]
        if not executables:
            issues.append(
                EnvironmentIssue("process_unverified", "Rule has no game process marker", True)
            )
        for name in executables:
            try:
                issues.extend(executable_guard(target_path(installation.path, name)).issues)
            except (OSError, ModMeshError):
                issues.append(
                    EnvironmentIssue(
                        "unsafe_executable", "Cannot validate the game process marker", True
                    )
                )
        return EnvironmentReport(tuple(issues))

    @property
    def _loader_required(self) -> bool:
        kinds = {item.kind for item in self.rule.install_strategies}
        return InstallStrategyKind.PLUGIN in kinds and kinds <= {
            InstallStrategyKind.PLUGIN,
            InstallStrategyKind.READONLY,
        }

    def support(
        self, installation: GameInstallation | None = None
    ) -> tuple[CapabilitySupport, ...]:
        reports = super().support(installation)
        result = []
        for report in reports:
            evidence = report.evidence + (
                CapabilityEvidence(
                    "game_rule",
                    f"{self.rule.game_id}@{self.rule.revision}:{self.rule.digest}",
                    self.entry.approved,
                ),
                CapabilityEvidence(
                    "rule_origin",
                    f"{self.entry.origin.value}:{self.entry.trust}",
                    self.entry.approved,
                ),
            )
            message = report.message
            state = report.state
            if report.capability in self.capabilities and state == SupportState.AVAILABLE:
                message = "규칙 기반 기능입니다. 실게임 모드 로딩은 미검증입니다."
            if report.capability in self.rule.capabilities and not self.entry.approved:
                message, state = "규칙 내용을 먼저 승인하세요.", SupportState.BLOCKED
            elif (
                report.capability in {GameCapability.INSTALL_RULES, GameCapability.PROFILES}
                and self._loader_required
                and (installation is None or installation.loader is None)
            ):
                message, state = (
                    "필요한 로더를 별도로 설치하고 다시 확인하세요.",
                    SupportState.BLOCKED,
                )
            result.append(replace(report, message=message, state=state, evidence=evidence))
        return tuple(result)

    def mod_locations(self, installation: GameInstallation) -> tuple[ModLocation, ...]:
        return tuple(
            ModLocation(root.root_id, target_path(installation.path, root.path), root.layout)
            for root in self.rule.mod_roots
        )

    def installation_rules(
        self, installation: GameInstallation, file: ModFile
    ) -> tuple[InstallRule, ...]:
        roots = {root.root_id: root for root in self.rule.mod_roots}
        return tuple(
            rule
            for specification in self.rule.install_strategies
            for rule in self.strategies.select(
                specification,
                roots[specification.root_id],
                file,
                loader_present=installation.loader is not None,
            )
        )

    def scan_mods(self, installation: GameInstallation) -> tuple[InstalledMod, ...]:
        if not self.validate(installation).valid:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Cannot scan an invalid rule-based installation"
            )
        found: list[InstalledMod] = []
        visited = 0
        for location in self.mod_locations(installation):
            if not location.path.is_dir():
                continue
            for current, directories, names in location.path.walk(
                on_error=_scan_error, follow_symlinks=False
            ):
                visited += 1
                if (
                    visited > 4096
                    or len(current.relative_to(location.path).parts) > 8
                    or len(found) + len(names) > 4096
                ):
                    raise ModMeshError(ErrorCode.VALIDATION, "Rule mod scan limit reached")
                directories[:] = sorted(
                    name
                    for name in directories
                    if not (current / name).is_symlink() and not (current / name).is_junction()
                )
                for name in sorted(names):
                    relative = (current / name).relative_to(installation.path).as_posix()
                    path = target_path(installation.path, relative)
                    if path.is_file():
                        identity = str(
                            uuid5(
                                NAMESPACE_URL,
                                f"{installation.installation_id}:{relative.casefold()}",
                            )
                        )
                        found.append(
                            InstalledMod(
                                installation.installation_id,
                                "external:" + identity,
                                None,
                                (InstalledFile(relative, False),),
                                name=name,
                                kind=location.layout,
                            )
                        )
        return tuple(found)

    def _validate_paths(self, paths: tuple[str, ...]) -> EnvironmentReport:
        issues = []
        for path in paths:
            allowed = any(
                path.startswith(root.path + "/")
                and Path(path).suffix.lower() in specification.extensions
                for root in self.rule.mod_roots
                for specification in self.rule.install_strategies
                if root.root_id == specification.root_id
                and specification.kind != InstallStrategyKind.READONLY
            )
            if not allowed:
                issues.append(
                    EnvironmentIssue(
                        "outside_mod_scope",
                        "Profile/removal path is outside writable rule scope",
                        True,
                    )
                )
        return EnvironmentReport(tuple(issues))

    def validate_profile_payload(
        self,
        installation: GameInstallation,
        files: tuple[tuple[ModFile, Path], ...],
        replaced: frozenset[str],
    ) -> EnvironmentReport:
        return self.validate_artifacts(installation, files)

    def validate_artifacts(
        self, installation: GameInstallation, files: tuple[tuple[ModFile, Path], ...]
    ) -> EnvironmentReport:
        return self.loader_strategy.inspect(installation, files)

    def validate_payload(
        self, installation: GameInstallation, rule: InstallRule, paths: tuple[str, ...]
    ) -> EnvironmentReport:
        specification = next(
            item for item in self.rule.install_strategies if item.strategy_id == rule.rule_id
        )
        if specification.kind == InstallStrategyKind.PLUGIN:
            return self.loader_strategy.payload(paths)
        return EnvironmentReport()

    def auxiliary_files(
        self, installation: GameInstallation, rule: InstallRule, paths: tuple[str, ...]
    ) -> dict[str, bytes]:
        specification = next(
            item for item in self.rule.install_strategies if item.strategy_id == rule.rule_id
        )
        if specification.kind == InstallStrategyKind.PLUGIN:
            return self.loader_strategy.auxiliary(paths)
        return {}

    def validate_profile(
        self,
        installation: GameInstallation,
        files: tuple[tuple[str, Path], ...],
        replaced: tuple[str, ...],
    ) -> EnvironmentReport:
        return self._validate_paths(tuple(path for path, _ in files) + replaced)

    def validate_removal(
        self,
        installation: GameInstallation,
        paths: tuple[str, ...],
        restored: tuple[Path, ...] = (),
    ) -> EnvironmentReport:
        return self._validate_paths(paths)


def _scan_error(error: OSError) -> None:
    raise error
