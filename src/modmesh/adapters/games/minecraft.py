from __future__ import annotations

import json
import os
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from modmesh.adapters.games.minecraft_mods import JarMetadata, read_jar, validate_native
from modmesh.adapters.loaders import MinecraftArtifactInspector
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import (
    DiscoveryContext,
    EnvironmentIssue,
    EnvironmentReport,
    GameAdapter,
    GameCapability,
    InstallRule,
    ModLocation,
)
from modmesh.core.planning import portable_path, target_path
from modmesh.core.processes import java_guard
from modmesh.domain.models import (
    Game,
    GameInstallation,
    GameVersion,
    InstalledFile,
    InstalledMod,
    Loader,
    ModFile,
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
        {
            GameCapability.DISCOVERY,
            GameCapability.VERSION,
            GameCapability.MOD_DISCOVERY,
            GameCapability.INSTALL_RULES,
            GameCapability.REMOVAL_RULES,
            GameCapability.PROFILES,
        }
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

    def loader(self, metadata: InstanceMetadata) -> Loader | None:
        mapping = {
            "net.fabricmc.fabric-loader": "fabric",
            "net.minecraftforge": "forge",
            "net.neoforged": "neoforge",
        }
        if metadata.components:
            component_values = {
                (mapping[str(item["uid"])], version_id(item.get("version")))
                for item in metadata.components
                if item.get("uid") in mapping
            }
            if len(component_values) == 1:
                name, version = next(iter(component_values))
                return Loader(name, version)
            return None
        results: set[Loader | None] = set()
        for chain in metadata.chains:
            values: set[Loader] = set()
            for record in chain:
                libraries = record.get("libraries", [])
                if not isinstance(libraries, list) or len(libraries) > 10000:
                    raise ModMeshError(ErrorCode.VALIDATION, "Invalid game library metadata")
                for library in libraries:
                    if not isinstance(library, dict) or not isinstance(library.get("name"), str):
                        raise ModMeshError(ErrorCode.VALIDATION, "Invalid game library entry")
                    parts = library["name"].split(":")
                    if len(parts) < 3:
                        continue
                    group, artifact, version = parts[:3]
                    if (group, artifact) == ("net.fabricmc", "fabric-loader"):
                        values.add(Loader("fabric", version_id(version)))
                    elif (group, artifact) == ("net.neoforged", "neoforge"):
                        values.add(Loader("neoforge", version_id(version)))
                    elif (group, artifact) == ("net.minecraftforge", "forge"):
                        prefix = (metadata.version.value or "") + "-"
                        if version.startswith(prefix):
                            values.add(Loader("forge", version_id(version[len(prefix) :])))
            results.add(next(iter(values)) if len(values) == 1 else None)
        return next(iter(results)) if len(results) == 1 else None

    def inspect(self, path: Path, platform: Platform) -> GameInstallation | None:
        metadata = self.metadata(path, platform)
        if metadata is None:
            return None
        return GameInstallation(
            self.game.game_id,
            metadata.path,
            "java",
            platform,
            metadata.version,
            self.loader(metadata),
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
        if metadata is not None and self.loader(metadata) != installation.loader:
            issues.append(EnvironmentIssue("changed_loader", "Instance loader changed", True))
        if installation.loader is None:
            issues.append(
                EnvironmentIssue(
                    "loader_unverified", "No single supported mod loader is identified"
                )
            )
        issues.append(
            EnvironmentIssue(
                "game_launch_unverified",
                "Local launcher metadata does not verify a successful game launch",
            )
        )
        return EnvironmentReport(tuple(issues))

    def validate_operation(self, installation: GameInstallation) -> EnvironmentReport:
        return java_guard(installation.path)

    def installation_rules(
        self, installation: GameInstallation, file: ModFile
    ) -> tuple[InstallRule, ...]:
        loader = installation.loader
        if (
            loader is None
            or loader.version is None
            or installation.version.value is None
            or loader.name not in {"fabric", "forge", "neoforge"}
            or not file.filename.casefold().endswith(".jar")
        ):
            return ()
        return (InstallRule("minecraft-" + loader.name, "mods", "file", frozenset({".jar"})),)

    def removal_rules(self, installation: GameInstallation) -> tuple[InstallRule, ...]:
        return (InstallRule("minecraft-managed", "mods", "file", frozenset({".jar"})),)

    def _jars(
        self, installation: GameInstallation, excluded: frozenset[str]
    ) -> tuple[tuple[tuple[str, JarMetadata], ...], tuple[EnvironmentIssue, ...]]:
        directory = target_path(installation.path, "mods")
        jars = []
        issues = []
        if directory.is_dir():
            for index, path in enumerate(directory.iterdir()):
                if index >= 4096:
                    raise ModMeshError(ErrorCode.VALIDATION, "Minecraft mod scan limit reached")
                relative = path.relative_to(installation.path).as_posix()
                if relative.casefold() in excluded or not path.name.casefold().endswith(".jar"):
                    continue
                try:
                    jars.append(
                        (
                            relative,
                            read_jar(
                                path, installation.loader.name if installation.loader else None
                            ),
                        )
                    )
                except (OSError, ModMeshError):
                    issues.append(
                        EnvironmentIssue(
                            "external_jar_unverified",
                            f"External JAR could not be verified: {path.name}",
                        )
                    )
        return tuple(jars), tuple(issues)

    def validate_artifacts(
        self, installation: GameInstallation, files: tuple[tuple[ModFile, Path], ...]
    ) -> EnvironmentReport:
        loader = installation.loader
        if loader is None:
            return EnvironmentReport(
                (EnvironmentIssue("missing_loader", "A supported loader must be identified", True),)
            )
        excluded = frozenset(("mods/" + file.filename).casefold() for file, _ in files)
        jars, warnings = self._jars(installation, excluded)
        report = MinecraftArtifactInspector().inspect(installation, files, existing=jars)
        return EnvironmentReport((*warnings, *report.issues))

    def validate_removal(
        self,
        installation: GameInstallation,
        paths: tuple[str, ...],
        restored: tuple[Path, ...] = (),
    ) -> EnvironmentReport:
        before, warnings = self._jars(installation, frozenset())
        after = tuple(
            (path, jar)
            for path, jar in before
            if path.casefold() not in {value.casefold() for value in paths}
        )
        restored_jars = tuple(
            (
                f"restored-{index}",
                read_jar(path, installation.loader.name if installation.loader else None),
            )
            for index, path in enumerate(restored)
        )
        after = (*after, *restored_jars)
        previous = {
            identity
            for _, jar in before
            for mod in jar.mods
            for identity in (mod.mod_id, *mod.aliases)
        }
        remaining = {
            identity
            for _, jar in after
            for mod in jar.mods
            for identity in (mod.mod_id, *mod.aliases)
        }
        return EnvironmentReport(
            (
                *warnings,
                *validate_native(installation, after, lost=frozenset(previous - remaining)).issues,
            )
        )

    def validate_profile_payload(
        self,
        installation: GameInstallation,
        files: tuple[tuple[ModFile, Path], ...],
        replaced: frozenset[str],
    ) -> EnvironmentReport:
        for _, path in files:
            self._selected_jar(installation, path)
        return EnvironmentReport(
            (
                EnvironmentIssue(
                    "profile_native_pending",
                    "Native dependencies are checked by the final profile switch plan",
                ),
            )
        )

    def _selected_jar(self, installation: GameInstallation, path: Path) -> JarMetadata:
        loader = installation.loader
        metadata = read_jar(path, loader.name if loader else None)
        if (
            loader
            and loader.name == "neoforge"
            and metadata.metadata_file != "META-INF/neoforge.mods.toml"
            and not (loader.version or "").startswith("20.4.")
        ):
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "This NeoForge version requires neoforge.mods.toml"
            )
        return metadata

    def profile_settings(self, installation: GameInstallation) -> tuple[str, ...]:
        return ("options.txt",)

    def validate_profile(
        self,
        installation: GameInstallation,
        files: tuple[tuple[str, Path], ...],
        replaced: tuple[str, ...],
    ) -> EnvironmentReport:
        existing, warnings = self._jars(
            installation, frozenset(path.casefold() for path in replaced)
        )
        selected = tuple(
            (relative, self._selected_jar(installation, path)) for relative, path in files
        )
        return EnvironmentReport(
            (*warnings, *validate_native(installation, (*existing, *selected)).issues)
        )

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
            native = None
            with suppress(ModMeshError):
                native = read_jar(path, installation.loader.name if installation.loader else None)
            primary = native.mods[0] if native and native.mods else None
            mods.append(
                InstalledMod(
                    installation.installation_id,
                    "external:" + identity,
                    primary.version if primary else None,
                    (InstalledFile(relative, False),),
                    name=primary.name if primary else name,
                    kind="java-jar",
                    enabled=not name.casefold().endswith(".disabled"),
                )
            )
        return tuple(sorted(mods, key=lambda mod: mod.name or ""))
