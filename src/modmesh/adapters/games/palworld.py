from collections.abc import Iterable
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from modmesh.adapters.games.steam import read_appmanifest, steam_installations
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
from modmesh.core.processes import executable_guard
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

APP_ID = "1623730"
_SHIPPING = Path("Pal/Binaries/Win64/Palworld-Win64-Shipping.exe")


class PalworldAdapter(GameAdapter):
    game = Game("palworld", "Palworld", ("steam",), frozenset({Platform.WINDOWS}))
    capabilities = frozenset(
        {
            GameCapability.DISCOVERY,
            GameCapability.VERSION,
            GameCapability.MOD_DISCOVERY,
            GameCapability.INSTALL_RULES,
        }
    )

    def candidates(self, context: DiscoveryContext) -> Iterable[Path]:
        return (
            *context.roots,
            *(installation.path for installation in steam_installations(context, APP_ID)),
        )

    def inspect(self, path: Path, platform: Platform) -> GameInstallation | None:
        path = path.resolve()
        if (
            platform != Platform.WINDOWS
            or not (path / "Palworld.exe").is_file()
            or not (path / _SHIPPING).is_file()
        ):
            return None
        version = GameVersion()
        manifest = path.parent.parent / f"appmanifest_{APP_ID}.acf"
        if manifest.is_file():
            try:
                steam = read_appmanifest(manifest, APP_ID)
                if steam.path == path:
                    version = GameVersion(
                        build_id=steam.build_id, evidence=f"steam-appmanifest:{APP_ID}"
                    )
            except (OSError, ModMeshError):
                pass
        loader = (
            Loader("ue4ss")
            if any(
                (path / directory / "UE4SS.dll").is_file()
                for directory in ("Pal/Binaries/Win64", "Pal/Binaries/Win64/ue4ss")
            )
            else None
        )
        return GameInstallation(self.game.game_id, path, "steam", platform, version, loader)

    def validate(self, installation: GameInstallation) -> EnvironmentReport:
        issues = list(super().validate(installation).issues)
        if (
            not (installation.path / _SHIPPING).is_file()
            or not (installation.path / "Palworld.exe").is_file()
        ):
            issues.append(
                EnvironmentIssue(
                    "missing_executable", "Palworld Steam Windows binaries are missing", True
                )
            )
        if installation.version.build_id is None:
            issues.append(
                EnvironmentIssue(
                    "unverified_steam_build",
                    "Steam build ID is unavailable; only the Windows Steam layout was identified",
                )
            )
        if (
            any(
                location.path.is_dir()
                for location in self.mod_locations(installation)
                if location.layout == "ue4ss"
            )
            and installation.loader is None
        ):
            issues.append(
                EnvironmentIssue(
                    "ue4ss_loader_missing", "UE4SS mod folders exist without a detected UE4SS DLL"
                )
            )
        return EnvironmentReport(tuple(issues))

    def validate_operation(self, installation: GameInstallation) -> EnvironmentReport:
        return executable_guard(installation.path / _SHIPPING)

    def mod_locations(self, installation: GameInstallation) -> tuple[ModLocation, ...]:
        root = installation.path
        return (
            ModLocation("Pak mods", root / "Pal/Content/Paks/~mods", "pak"),
            ModLocation("Blueprint mods", root / "Pal/Content/Paks/LogicMods", "blueprint-pak"),
            ModLocation("UE4SS legacy mods", root / "Pal/Binaries/Win64/Mods", "ue4ss"),
            ModLocation("UE4SS nested mods", root / "Pal/Binaries/Win64/ue4ss/Mods", "ue4ss"),
        )

    def installation_rules(
        self, installation: GameInstallation, file: ModFile
    ) -> tuple[InstallRule, ...]:
        archive = file.filename.lower().endswith(".zip")
        if not archive and not file.filename.lower().endswith(".pak"):
            return ()
        layout = "archive" if archive else "file"
        extensions = frozenset({".pak", ".utoc", ".ucas"}) if archive else frozenset({".pak"})
        docs = frozenset({"readme.md", "readme.txt", "license", "license.txt", "changelog.txt"})
        rules = [
            InstallRule("palworld-pak", "Pal/Content/Paks/~mods", layout, extensions, docs),
            InstallRule(
                "palworld-blueprint", "Pal/Content/Paks/LogicMods", layout, extensions, docs
            ),
        ]
        if archive and installation.loader is not None and installation.loader.name == "ue4ss":
            base = (
                "Pal/Binaries/Win64/ue4ss"
                if (installation.path / "Pal/Binaries/Win64/ue4ss/UE4SS.dll").is_file()
                else "Pal/Binaries/Win64"
            )
            rules.append(
                InstallRule("palworld-ue4ss", base + "/Mods", "archive", frozenset({"*"}), docs)
            )
        return tuple(rules)

    def validate_payload(
        self, installation: GameInstallation, rule: InstallRule, paths: tuple[str, ...]
    ) -> EnvironmentReport:
        issues = []
        folded = {path.casefold() for path in paths}
        if rule.rule_id == "palworld-ue4ss":
            issues.append(
                EnvironmentIssue(
                    "ue4ss_order", "enabled.txt activation does not configure UE4SS load ordering"
                )
            )
            roots = {path.split("/")[0] for path in folded}
            for root in roots:
                if not any(
                    f"{root}/{marker}" in folded for marker in ("scripts/main.lua", "dlls/main.dll")
                ):
                    issues.append(
                        EnvironmentIssue(
                            "ue4ss_layout",
                            "UE4SS mods need Scripts/main.lua or dlls/main.dll in a mod directory",
                            True,
                        )
                    )
        else:
            if any("/" in path for path in paths):
                issues.append(
                    EnvironmentIssue(
                        "pak_layout",
                        "Pak packages require a flat payload; choose an explicit archive prefix",
                        True,
                    )
                )
            for path in folded:
                if path.endswith((".utoc", ".ucas")) and (
                    path.rsplit(".", 1)[0] + ".utoc" not in folded
                    or path.rsplit(".", 1)[0] + ".ucas" not in folded
                ):
                    issues.append(
                        EnvironmentIssue(
                            "iostore_pair",
                            "IoStore payload requires both .utoc and .ucas files",
                            True,
                        )
                    )
        return EnvironmentReport(tuple(issues))

    def auxiliary_files(
        self, installation: GameInstallation, rule: InstallRule, paths: tuple[str, ...]
    ) -> dict[str, bytes]:
        if rule.rule_id != "palworld-ue4ss":
            return {}
        folded = {path.casefold() for path in paths}
        roots = {path.split("/")[0] for path in paths}
        return {
            root + "/enabled.txt": b""
            for root in roots
            if root.casefold() + "/enabled.txt" not in folded
        }

    def scan_mods(self, installation: GameInstallation) -> tuple[InstalledMod, ...]:
        if not self.validate(installation).valid:
            raise ModMeshError(ErrorCode.VALIDATION, "Cannot scan an invalid Palworld installation")
        found: list[InstalledMod] = []
        root = installation.path.resolve()

        def safe(path: Path) -> bool:
            return (
                not path.is_symlink()
                and not path.is_junction()
                and path.resolve().is_relative_to(root)
            )

        def record(
            name: str, kind: str, files: tuple[Path, ...], enabled: bool | None = None
        ) -> None:
            relative_files = tuple(
                InstalledFile(path.relative_to(root).as_posix(), False) for path in files
            )
            identity = str(
                uuid5(
                    NAMESPACE_URL,
                    f"{installation.installation_id}:{relative_files[0].path.casefold()}",
                )
            )
            found.append(
                InstalledMod(
                    installation.installation_id,
                    f"external:{identity}",
                    None,
                    relative_files,
                    name=name,
                    kind=kind,
                    enabled=enabled,
                )
            )

        try:
            for location in self.mod_locations(installation):
                if not location.path.is_dir() or not safe(location.path):
                    continue
                entries = []
                for count, entry in enumerate(location.path.iterdir()):
                    if count >= 4096:
                        raise ModMeshError(
                            ErrorCode.VALIDATION, "Palworld mod directory scan limit reached"
                        )
                    if safe(entry):
                        entries.append(entry)
                entries.sort(key=lambda path: path.name.casefold())
                if location.layout != "ue4ss":
                    groups: dict[str, list[Path]] = {}
                    for entry in entries:
                        if entry.is_file() and entry.suffix.lower() in {".pak", ".utoc", ".ucas"}:
                            groups.setdefault(entry.stem.casefold(), []).append(entry)
                    for files in groups.values():
                        record(files[0].stem, location.layout, tuple(files))
                    continue
                states = _enabled_states(location.path / "mods.txt")
                for directory in entries:
                    if not directory.is_dir() or not any(
                        (directory / marker).is_file() and safe(directory / marker)
                        for marker in ("Scripts/main.lua", "dlls/main.dll")
                    ):
                        continue
                    files = []
                    for current, dirs, names in directory.walk(
                        on_error=_raise_os_error, follow_symlinks=False
                    ):
                        dirs[:] = sorted(name for name in dirs if safe(current / name))
                        if len(current.relative_to(directory).parts) >= 8:
                            if dirs:
                                raise ModMeshError(
                                    ErrorCode.VALIDATION, "UE4SS mod scan depth limit reached"
                                )
                            dirs[:] = []
                        for name in sorted(names):
                            file = current / name
                            if safe(file) and file.is_file():
                                files.append(file)
                            if len(files) > 5000:
                                raise ModMeshError(
                                    ErrorCode.VALIDATION, "UE4SS mod file scan limit reached"
                                )
                    if files:
                        marker = directory / "enabled.txt"
                        enabled = (
                            True
                            if marker.is_file() and safe(marker)
                            else states.get(directory.name.casefold())
                        )
                        record(directory.name, "ue4ss", tuple(files), enabled)
        except (OSError, UnicodeError) as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Cannot read Palworld mod directories") from exc
        return tuple(found)


def _enabled_states(path: Path) -> dict[str, bool | None]:
    if not path.is_file() or path.is_symlink():
        return {}
    with path.open("r", encoding="utf-8-sig") as stream:
        text = stream.read(1024 * 1024 + 1)
    if len(text) > 1024 * 1024:
        raise ModMeshError(ErrorCode.VALIDATION, "UE4SS configuration size limit reached")
    states: dict[str, bool | None] = {}
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.rsplit(":", 1))
        if name and value in {"0", "1"}:
            key = name.casefold()
            states[key] = None if key in states else value == "1"
    return states


def _raise_os_error(error: OSError) -> None:
    raise error
