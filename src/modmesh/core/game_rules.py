import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum

from modmesh.core.capabilities import GameCapability
from modmesh.domain.models import Platform

RULE_SCHEMA_VERSION = 1
RULE_MAX_BYTES = 65536


class InstallStrategyKind(StrEnum):
    FILE = "file"
    ARCHIVE = "archive"
    MAPPED = "mapped"
    PLUGIN = "plugin"
    READONLY = "readonly"


class LoaderKind(StrEnum):
    NONE = "none"
    BEPINEX = "bepinex"
    UE4SS = "ue4ss"
    LEGACY_MINECRAFT = "legacy-minecraft"


class LaunchKind(StrEnum):
    MANUAL = "manual"
    STEAM = "steam"
    EXECUTABLE = "executable"


@dataclass(frozen=True)
class StoreIds:
    steam: str | None = None


@dataclass(frozen=True)
class DetectionRule:
    required_files: tuple[str, ...]
    steam_manifest_required: bool = False


@dataclass(frozen=True)
class VersionRule:
    strategy: str = "unknown"


@dataclass(frozen=True)
class LoaderRule:
    strategy: LoaderKind = LoaderKind.NONE
    markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModRootRule:
    root_id: str
    path: str
    layout: str


@dataclass(frozen=True)
class PathMapping:
    source_prefix: str
    target_prefix: str


@dataclass(frozen=True)
class InstallStrategyRule:
    strategy_id: str
    kind: InstallStrategyKind
    root_id: str
    extensions: tuple[str, ...]
    mappings: tuple[PathMapping, ...] = ()


@dataclass(frozen=True)
class LaunchRule:
    strategy: LaunchKind = LaunchKind.MANUAL
    executable: str | None = None
    app_id: str | None = None
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class GameRule:
    schema_version: int
    game_id: str
    name: str
    revision: int
    edition: str
    platforms: tuple[Platform, ...]
    store_ids: StoreIds
    detect: DetectionRule
    game_version: VersionRule
    loader: LoaderRule
    mod_roots: tuple[ModRootRule, ...]
    install_strategies: tuple[InstallStrategyRule, ...]
    mod_types: tuple[str, ...]
    profile_scope: str
    launch: LaunchRule
    capabilities: tuple[GameCapability, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
