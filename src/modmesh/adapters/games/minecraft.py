from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import (
    DiscoveryContext,
    EnvironmentIssue,
    EnvironmentReport,
    GameAdapter,
    GameCapability,
    ModLocation,
)
from modmesh.core.planning import portable_path, target_path
from modmesh.domain.models import (
    Game,
    GameInstallation,
    GameVersion,
    InstalledFile,
    InstalledMod,
    Platform,
)


def read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ModMeshError(ErrorCode.VALIDATION, "Launcher metadata must be a regular file")
    try:
        with path.open("rb") as stream:
            payload = stream.read(2 * 1024**2 + 1)
        if len(payload) > 2 * 1024**2:
            raise ModMeshError(ErrorCode.VALIDATION, "Launcher metadata exceeds the size limit")
        data = json.loads(payload)
    except (OSError, ValueError, RecursionError) as exc:
        raise ModMeshError(ErrorCode.VALIDATION, "Cannot read launcher metadata") from exc
    if not isinstance(data, dict):
        raise ModMeshError(ErrorCode.VALIDATION, "Launcher metadata must be an object")
    return dict(data)


def version_id(value: object) -> str:
    if not isinstance(value, str) or "/" in portable_path(value) or len(value) > 128:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid local game version identifier")
    return value


def launcher_roots(context: DiscoveryContext) -> tuple[Path, ...]:
    home, env = context.home, context.environment
    roots: tuple[Path, ...]
    if context.platform == Platform.WINDOWS:
        appdata = Path(env.get("APPDATA", str(home / "AppData/Roaming")))
        roots = (
            appdata / ".minecraft",
            appdata / "PrismLauncher",
            home / "scoop/persist/prismlauncher",
        )
    elif context.platform == Platform.MACOS:
        roots = (
            home / "Library/Application Support/minecraft",
            home / "Library/Application Support/PrismLauncher",
        )
    else:
        roots = (
            home / ".minecraft",
            Path(env.get("XDG_DATA_HOME", str(home / ".local/share"))) / "PrismLauncher",
            home / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher",
        )
    return (*context.roots, *roots)


@dataclass(frozen=True)
class LauncherProfile:
    game_directory: Path
    version_id: str | None
    profile_id: str


def profiles(root: Path) -> tuple[LauncherProfile, ...]:
    result = []
    for name in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json"):
        path = root / name
        if not path.exists():
            continue
        data = read_json(path).get("profiles", {})
        if not isinstance(data, dict) or len(data) > 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid launcher profile list")
        for key, item in data.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid launcher profile entry")
            directory = item.get("gameDir")
            if directory is not None and (
                not isinstance(directory, str) or not Path(directory).is_absolute()
            ):
                raise ModMeshError(ErrorCode.VALIDATION, "Launcher game directory must be absolute")
            selected = item.get("lastVersionId")
            selected = version_id(selected) if selected is not None else None
            result.append(
                LauncherProfile(
                    Path(directory).resolve() if directory else root.resolve(), selected, key
                )
            )
    return tuple(result)


def version_chain(root: Path, selected: str) -> tuple[dict[str, object], ...]:
    chain: list[dict[str, object]] = []
    seen: set[str] = set()
    while selected:
        selected = version_id(selected)
        if selected in seen or len(seen) >= 8:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Game version inheritance is cyclic or too deep"
            )
        seen.add(selected)
        path = target_path(root, f"versions/{selected}/{selected}.json")
        if not path.exists():
            return tuple(chain)
        data = read_json(path)
        if data.get("id") != selected:
            raise ModMeshError(ErrorCode.VALIDATION, "Game version metadata identity mismatch")
        chain.append(data)
        parent = data.get("inheritsFrom")
        selected = version_id(parent) if parent is not None else ""
    return tuple(chain)


@dataclass(frozen=True)
class InstanceMetadata:
    path: Path
    version: GameVersion
    launcher: str
    components: tuple[dict[str, object], ...] = ()
    chains: tuple[tuple[dict[str, object], ...], ...] = ()


class MinecraftAdapter(GameAdapter):
    game = Game("minecraft", "Minecraft Java Edition", ("java",), frozenset(Platform))
    capabilities = frozenset(
        {GameCapability.DISCOVERY, GameCapability.VERSION, GameCapability.MOD_DISCOVERY}
    )

    def __init__(self, roots: tuple[Path, ...] = ()) -> None:
        self.roots = roots

    def candidates(self, context: DiscoveryContext) -> Iterable[Path]:
        for root in (*self.roots, *launcher_roots(context)):
            if root.is_symlink() or root.is_junction():
                continue
            yield root
            if any(
                (root / name).is_file()
                for name in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json")
            ):
                yield from (profile.game_directory for profile in profiles(root))
            instances = root if root.name == "instances" else root / "instances"
            if instances.is_dir() and not instances.is_symlink() and not instances.is_junction():
                for index, instance in enumerate(instances.iterdir()):
                    if index >= 256:
                        raise ModMeshError(
                            ErrorCode.VALIDATION, "Minecraft instance scan limit reached"
                        )
                    if (
                        instance.is_dir()
                        and not instance.is_symlink()
                        and not instance.is_junction()
                    ):
                        yield instance

    def metadata(self, path: Path, platform: Platform) -> InstanceMetadata | None:
        if path.is_symlink() or path.is_junction() or not path.is_dir():
            return None
        path = path.resolve()
        instance = path if (path / "mmc-pack.json").is_file() else path.parent
        if (instance / "mmc-pack.json").is_file():
            cfg = instance / "instance.cfg"
            if not cfg.is_file() or cfg.is_symlink():
                return None
            pack = read_json(instance / "mmc-pack.json")
            values = pack.get("components")
            if pack.get("formatVersion") != 1 or not isinstance(values, list) or len(values) > 128:
                raise ModMeshError(ErrorCode.VALIDATION, "Unsupported instance component manifest")
            components = tuple(dict(value) for value in values if isinstance(value, dict))
            if len(components) != len(values):
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid instance component")
            minecraft = [value for value in components if value.get("uid") == "net.minecraft"]
            if len(minecraft) != 1:
                raise ModMeshError(ErrorCode.VALIDATION, "Instance needs one Minecraft component")
            version = version_id(minecraft[0].get("version"))
            directory = "minecraft" if (instance / "minecraft").exists() else ".minecraft"
            game = target_path(instance, directory)
            if not game.is_dir() or path not in {instance, game}:
                return None
            return InstanceMetadata(
                game,
                GameVersion(version, evidence="prism-multimc:net.minecraft"),
                "prism-multimc",
                components,
            )
        context = DiscoveryContext(platform, environment=os.environ)
        anchors = tuple(
            dict.fromkeys((path, *self.roots, *launcher_roots(context), *tuple(path.parents)[:3]))
        )
        for root in anchors:
            if not any(
                (root / name).is_file()
                for name in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json")
            ):
                continue
            selected = tuple(
                profile for profile in profiles(root) if profile.game_directory == path
            )
            if not selected and path != root:
                continue
            chains = tuple(
                version_chain(root, profile.version_id)
                for profile in selected
                if profile.version_id is not None
            )
            versions = set()
            for chain in chains:
                if chain:
                    last = chain[-1]
                    if (
                        "inheritsFrom" not in last
                        and last.get("type") in {"release", "snapshot"}
                        and last.get("mainClass")
                        in {"net.minecraft.client.main.Main", "net.minecraft.launchwrapper.Launch"}
                    ):
                        versions.add(version_id(last.get("id")))
            exact = (
                next(iter(versions))
                if len(versions) == 1
                and len(chains) == len(selected)
                and all(chain and "inheritsFrom" not in chain[-1] for chain in chains)
                else None
            )
            return InstanceMetadata(
                path,
                GameVersion(exact, evidence="minecraft-launcher:local-profiles"),
                "minecraft-launcher",
                chains=chains,
            )
        return None

    def inspect(self, path: Path, platform: Platform) -> GameInstallation | None:
        metadata = self.metadata(path, platform)
        if metadata is None:
            return None
        return GameInstallation(
            self.game.game_id, metadata.path, "java", platform, metadata.version
        )

    def validate(self, installation: GameInstallation) -> EnvironmentReport:
        issues = list(super().validate(installation).issues)
        metadata = self.metadata(installation.path, installation.platform)
        if metadata is None:
            issues.append(
                EnvironmentIssue("missing_instance", "Minecraft launcher metadata is missing", True)
            )
        elif metadata.version != installation.version:
            issues.append(
                EnvironmentIssue("changed_instance", "Minecraft instance version changed", True)
            )
        issues.append(
            EnvironmentIssue(
                "loader_unverified", "Loader support is not verified for this instance"
            )
        )
        issues.append(
            EnvironmentIssue(
                "game_launch_unverified",
                "Local launcher metadata does not verify a successful game launch",
            )
        )
        return EnvironmentReport(tuple(issues))

    def mod_locations(self, installation: GameInstallation) -> tuple[ModLocation, ...]:
        return (ModLocation("Java mods", installation.path / "mods", "java-jar"),)

    def scan_mods(self, installation: GameInstallation) -> tuple[InstalledMod, ...]:
        root = installation.path
        directory = target_path(root, "mods")
        if not directory.is_dir():
            return ()
        mods = []
        for index, path in enumerate(directory.iterdir()):
            if index >= 4096:
                raise ModMeshError(ErrorCode.VALIDATION, "Minecraft mod scan limit reached")
            if path.is_symlink() or path.is_junction() or not path.is_file():
                continue
            name = path.name
            if not name.casefold().endswith((".jar", ".jar.disabled")):
                continue
            relative = path.relative_to(root).as_posix()
            identity = str(
                uuid5(NAMESPACE_URL, f"{installation.installation_id}:{relative.casefold()}")
            )
            mods.append(
                InstalledMod(
                    installation.installation_id,
                    "external:" + identity,
                    None,
                    (InstalledFile(relative, False),),
                    name=name,
                    kind="java-jar",
                    enabled=not name.casefold().endswith(".disabled"),
                )
            )
        return tuple(sorted(mods, key=lambda mod: mod.name or ""))
