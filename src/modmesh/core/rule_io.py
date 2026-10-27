import json
import re
from pathlib import Path, PurePosixPath

from modmesh.core.capabilities import GameCapability
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import (
    RULE_MAX_BYTES,
    RULE_SCHEMA_VERSION,
    DetectionRule,
    GameRule,
    InstallStrategyKind,
    InstallStrategyRule,
    LaunchKind,
    LaunchRule,
    LoaderKind,
    LoaderRule,
    ModRootRule,
    PathMapping,
    StoreIds,
    VersionRule,
)
from modmesh.core.planning import portable_path
from modmesh.domain.models import Platform
from modmesh.domain.validation import require_id

_PROTECTED = {
    "saved",
    "saves",
    "savegames",
    "steamapps",
    "userdata",
    "windows",
    "appdata",
    ".git",
    ".modmesh-state-v1",
}
_EXECUTABLES = {
    "cmd",
    "powershell",
    "pwsh",
    "python",
    "pythonw",
    "python3",
    "node",
    "wscript",
    "cscript",
    "bash",
    "sh",
    "mshta",
    "rundll32",
    "regsvr32",
    "java",
    "javaw",
    "conhost",
    "wt",
    "reg",
    "schtasks",
    "explorer",
    "msiexec",
}
_FORBIDDEN_EXTENSIONS = {
    ".exe",
    ".com",
    ".bat",
    ".cmd",
    ".ps1",
    ".py",
    ".pyw",
    ".js",
    ".vbs",
    ".sh",
    ".lnk",
    ".url",
    ".msi",
    ".reg",
    ".scr",
}
_ARGUMENTS = {"--fullscreen", "--windowed", "-fullscreen", "-windowed", "-novid", "-nojoy"}


def _fail(message: str) -> ModMeshError:
    return ModMeshError(ErrorCode.VALIDATION, message)


def _fields(value: object, allowed: str, required: str = "") -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _fail("Rule section must be an object")
    result: dict[str, object] = dict(value)
    if set(result) - set(allowed.split()) or set(required.split()) - set(result):
        raise _fail("Rule has unknown or missing fields")
    return result


def _text(value: object, *, limit: int = 240, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not value.strip() and not empty)
        or any(not char.isprintable() for char in value)
    ):
        raise _fail("Rule text is empty, too long or contains control characters")
    return value


def _identifier(value: object) -> str:
    text = _text(value, limit=64)
    require_id(text, "rule identifier")
    return text


def _array(value: object, limit: int = 32) -> list[object]:
    if not isinstance(value, list) or len(value) > limit:
        raise _fail("Rule collection exceeds its limit or is not an array")
    return list(value)


def _strings(value: object, limit: int = 32) -> tuple[str, ...]:
    result = tuple(_text(item) for item in _array(value, limit))
    if len({item.casefold() for item in result}) != len(result):
        raise _fail("Rule collection contains duplicate values")
    return result


def _path(value: object, *, empty: bool = False) -> str:
    text = _text(value, empty=empty)
    if empty and text == "":
        return text
    if any(char in text for char in "${}%"):
        raise _fail("Rule paths cannot contain variables or templates")
    path = portable_path(text)
    if any(part.casefold() in _PROTECTED for part in path.split("/")):
        raise _fail("Rule path overlaps protected game or system content")
    return path


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _fail("Rule flag must be a boolean")
    return value


def _number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
        raise _fail("Rule revision/schema must be a bounded positive integer")
    return value


def _app_id(value: object) -> str | None:
    if value is None:
        return None
    text = _text(value, limit=10)
    if re.fullmatch(r"[1-9][0-9]{0,9}", text) is None or int(text) >= 2**32:
        raise _fail("Invalid Steam application ID")
    return text


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("Duplicate JSON key in game rule")
        result[key] = value
    return result


def _bounded(value: object, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 12 or budget[0] > 4096:
        raise _fail("Rule exceeds nesting or node limits")
    if isinstance(value, dict):
        if len(value) > 32:
            raise _fail("Rule object has too many fields")
        for key, item in value.items():
            _text(key, limit=64)
            _bounded(item, depth + 1, budget)
    elif isinstance(value, list):
        if len(value) > 64:
            raise _fail("Rule collection has too many items")
        for item in value:
            _bounded(item, depth + 1, budget)
    elif isinstance(value, str):
        _text(value, limit=512, empty=True)
    elif value is not None and not isinstance(value, (bool, int)):
        raise _fail("Rule values must be strings, booleans, integers or null")


def parse_rule(text: str) -> GameRule:
    try:
        if len(text.encode("utf-8")) > RULE_MAX_BYTES:
            raise _fail("Rule exceeds 64 KiB limit")
        value: object = json.loads(text, object_pairs_hook=_pairs)
        _bounded(value)
        return _parse(value)
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise _fail("Invalid game rule JSON or field value") from exc


def read_rule(path: Path) -> GameRule:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise _fail("Rule must be a regular JSON file")
    try:
        with path.open("rb") as stream:
            data = stream.read(RULE_MAX_BYTES + 1)
        if len(data) > RULE_MAX_BYTES:
            raise _fail("Rule exceeds 64 KiB limit")
        return parse_rule(data.decode("utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise _fail("Cannot read a UTF-8 game rule") from exc


def _parse(value: object) -> GameRule:
    names = (
        "schema_version game_id name revision edition platforms store_ids detect game_version "
        "loader mod_roots install_strategies mod_types profile_scope launch capabilities"
    )
    data = _fields(value, names, names)
    schema = _number(data["schema_version"])
    if schema != RULE_SCHEMA_VERSION:
        raise _fail("Unsupported game rule schema")
    platforms = tuple(Platform(item) for item in _strings(data["platforms"], 3))
    if not platforms:
        raise _fail("Rule needs a platform")
    stores = _fields(data["store_ids"], "steam")
    store = StoreIds(_app_id(stores.get("steam")))
    detection = _fields(data["detect"], "required_files steam_manifest_required", "required_files")
    required = tuple(_path(item) for item in _strings(detection["required_files"], 16))
    if not required or len({item.casefold() for item in required}) != len(required):
        raise _fail("Rule needs unambiguous required game files")
    detect = DetectionRule(required, _boolean(detection.get("steam_manifest_required", False)))
    version = _fields(data["game_version"], "strategy", "strategy")
    version_strategy = _text(version["strategy"])
    if version_strategy not in {"unknown", "steam-build"}:
        raise _fail("Unknown game version strategy")
    if (
        detect.steam_manifest_required or version_strategy == "steam-build"
    ) and store.steam is None:
        raise _fail("Steam detection/version requires a Steam application ID")
    raw_loader = _fields(data["loader"], "strategy markers", "strategy")
    loader = LoaderRule(
        LoaderKind(_text(raw_loader["strategy"])),
        tuple(_path(item) for item in _strings(raw_loader.get("markers", []), 16)),
    )
    if loader.strategy in {LoaderKind.BEPINEX, LoaderKind.UE4SS} and not loader.markers:
        raise _fail("Loader strategy requires explicit marker paths")
    if loader.strategy == LoaderKind.NONE and loader.markers:
        raise _fail("A rule without a loader cannot declare loader markers")
    marker_names = {
        LoaderKind.BEPINEX: {"bepinex.dll", "bepinex.core.dll"},
        LoaderKind.UE4SS: {"ue4ss.dll"},
    }
    if loader.strategy in marker_names and any(
        PurePosixPath(marker).name.casefold() not in marker_names[loader.strategy]
        for marker in loader.markers
    ):
        raise _fail("Loader markers must name the known loader DLL")
    roots = []
    for raw in _array(data["mod_roots"], 16):
        root_data = _fields(raw, "root_id path layout", "root_id path layout")
        layout = _identifier(root_data["layout"])
        if layout not in {
            "files",
            "pak",
            "blueprint-pak",
            "ue4ss",
            "bepinex",
            "jar",
            "steam-workshop",
        }:
            raise _fail("Unknown mod root layout")
        roots.append(
            ModRootRule(_identifier(root_data["root_id"]), _path(root_data["path"]), layout)
        )
    if len({root.root_id for root in roots}) != len(roots):
        raise _fail("Duplicate mod root ID")
    root_paths = [root.path.casefold() for root in roots]
    if any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for index, a in enumerate(root_paths)
        for b in root_paths[index + 1 :]
    ):
        raise _fail("Overlapping mod roots are ambiguous")
    for root in roots:
        if any(
            marker.casefold() == root.path.casefold()
            or marker.casefold().startswith(root.path.casefold() + "/")
            for marker in required + loader.markers
        ):
            raise _fail("Mod root overlaps a required game or loader file")
    strategies = []
    for raw in _array(data["install_strategies"], 32):
        item = _fields(
            raw,
            "strategy_id kind root_id extensions mappings",
            "strategy_id kind root_id extensions",
        )
        kind = InstallStrategyKind(_text(item["kind"]))
        root_id = _identifier(item["root_id"])
        if root_id not in {root.root_id for root in roots}:
            raise _fail("Strategy refers to a missing mod root")
        extensions = _strings(item["extensions"], 32)
        if kind != InstallStrategyKind.READONLY and not extensions:
            raise _fail("Writing strategies need explicit extensions")
        if any(
            re.fullmatch(r"\.[a-z0-9]{1,12}", ext) is None or ext in _FORBIDDEN_EXTENSIONS
            for ext in extensions
        ):
            raise _fail("Unsafe or unknown artifact extension")
        mappings = []
        for raw_mapping in _array(item.get("mappings", []), 16):
            mapping = _fields(
                raw_mapping, "source_prefix target_prefix", "source_prefix target_prefix"
            )
            mappings.append(
                PathMapping(
                    _path(mapping["source_prefix"]), _path(mapping["target_prefix"], empty=True)
                )
            )
        if bool(mappings) != (kind == InstallStrategyKind.MAPPED):
            raise _fail("Only mapped strategies require prefix mappings")
        prefixes = [mapping.source_prefix.casefold() for mapping in mappings]
        if any(
            a == b or a.startswith(b + "/") or b.startswith(a + "/")
            for index, a in enumerate(prefixes)
            for b in prefixes[index + 1 :]
        ):
            raise _fail("Overlapping source mappings are ambiguous")
        if kind == InstallStrategyKind.PLUGIN and loader.strategy == LoaderKind.NONE:
            raise _fail("Plugin placement requires a loader strategy")
        if (
            kind == InstallStrategyKind.PLUGIN
            and loader.strategy == LoaderKind.BEPINEX
            and ".dll" not in extensions
        ):
            raise _fail("BepInEx plugin strategy must permit DLL files")
        if (
            kind == InstallStrategyKind.PLUGIN
            and loader.strategy == LoaderKind.UE4SS
            and (".txt" not in extensions or not {".lua", ".dll"} & set(extensions))
        ):
            raise _fail("UE4SS plugins need a script/DLL and an enabled.txt marker")
        strategies.append(
            InstallStrategyRule(
                _identifier(item["strategy_id"]), kind, root_id, extensions, tuple(mappings)
            )
        )
    if len({item.strategy_id for item in strategies}) != len(strategies):
        raise _fail("Duplicate installation strategy ID")
    scope = _text(data["profile_scope"])
    if scope not in {"none", "managed-files"}:
        raise _fail("Unsupported profile scope")
    raw_launch = _fields(data["launch"], "strategy executable app_id arguments", "strategy")
    executable = (
        _path(raw_launch["executable"]) if raw_launch.get("executable") is not None else None
    )
    launch = LaunchRule(
        LaunchKind(_text(raw_launch["strategy"])),
        executable,
        _app_id(raw_launch.get("app_id")),
        _strings(raw_launch.get("arguments", []), 8),
    )
    if any(argument not in _ARGUMENTS for argument in launch.arguments):
        raise _fail("Launch arguments are outside the allowed game flags")
    if launch.strategy == LaunchKind.MANUAL and (executable or launch.app_id or launch.arguments):
        raise _fail("Manual launch cannot carry an executable, App ID or arguments")
    if launch.strategy == LaunchKind.STEAM and (
        not launch.app_id or launch.app_id != store.steam or executable or launch.arguments
    ):
        raise _fail("Steam launch must use the declared store App ID without arguments")
    if launch.strategy == LaunchKind.EXECUTABLE and (
        executable not in required
        or launch.app_id is not None
        or executable is None
        or PurePosixPath(executable).suffix.lower() != ".exe"
        or PurePosixPath(executable).stem.casefold() in _EXECUTABLES
        or Platform.WINDOWS not in platforms
    ):
        raise _fail(
            "Direct launch requires a detected Windows game executable, never an interpreter"
        )
    caps = tuple(
        GameCapability(item) for item in _strings(data["capabilities"], len(GameCapability))
    )
    writable = any(item.kind != InstallStrategyKind.READONLY for item in strategies)
    if GameCapability.DISCOVERY not in caps or {GameCapability.LAUNCH, GameCapability.SYNC} & set(
        caps
    ):
        raise _fail("Rule must declare discovery and cannot claim unimplemented launch/sync")
    mutating = any(
        cap in caps
        for cap in (
            GameCapability.INSTALL_RULES,
            GameCapability.REMOVAL_RULES,
            GameCapability.PROFILES,
        )
    )
    if mutating and (
        not writable or not any(PurePosixPath(path).suffix.lower() == ".exe" for path in required)
    ):
        raise _fail("File changes require a writable strategy and an explicit game process marker")
    if GameCapability.MOD_DISCOVERY in caps and not roots:
        raise _fail("Mod discovery requires mod roots")
    if (GameCapability.PROFILES in caps) != (scope == "managed-files"):
        raise _fail("Profile capability and scope disagree")
    if GameCapability.WORKSHOP_HANDOFF in caps and (store.steam is None or writable):
        raise _fail("Workshop handoff must be Steam-managed and read-only")
    mod_types = tuple(_identifier(item) for item in _strings(data["mod_types"], 16))
    return GameRule(
        schema,
        _identifier(data["game_id"]),
        _text(data["name"], limit=100),
        _number(data["revision"]),
        _identifier(data["edition"]),
        platforms,
        store,
        detect,
        VersionRule(version_strategy),
        loader,
        tuple(roots),
        tuple(strategies),
        mod_types,
        scope,
        launch,
        caps,
    )
