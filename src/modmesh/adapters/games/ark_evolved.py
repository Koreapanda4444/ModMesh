from __future__ import annotations

import configparser
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from modmesh.adapters.games.steam import read_appmanifest, read_keyvalues, steam_installations
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import (
    DiscoveryContext,
    EnvironmentIssue,
    EnvironmentReport,
    GameAdapter,
    GameCapability,
    ModLocation,
)
from modmesh.domain.models import (
    Game,
    GameInstallation,
    GameVersion,
    InstalledFile,
    InstalledMod,
    Platform,
    SourceRef,
)

APP_ID = "346110"
EXECUTABLE = "ShooterGame/Binaries/Win64/ShooterGame.exe"
MODS = "ShooterGame/Content/Mods"
SETTINGS = "ShooterGame/Saved/Config/WindowsNoEditor/GameUserSettings.ini"
WORKSHOP = "https://steamcommunity.com/workshop/browse/?appid=346110"


def workshop_id(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value) or int(value) >= 2**64:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Steam Workshop item ID")
    return value


def item_url(value: str) -> str:
    return "https://steamcommunity.com/sharedfiles/filedetails/?id=" + workshop_id(value)


def safe(path: Path, root: Path) -> bool:
    return (
        not path.is_symlink()
        and not path.is_junction()
        and path.resolve().is_relative_to(root.resolve())
    )


def numeric(value: str) -> bool:
    try:
        workshop_id(value)
        return True
    except ModMeshError:
        return False


def children(path: Path, root: Path) -> tuple[Path, ...]:
    if not path.exists():
        return ()
    if not safe(path, root) or not path.is_dir():
        raise ModMeshError(ErrorCode.VALIDATION, "Workshop directory escapes its installation")
    result = []
    for index, item in enumerate(path.iterdir()):
        if index >= 8192:
            raise ModMeshError(ErrorCode.VALIDATION, "Workshop scan limit reached")
        if safe(item, root):
            result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class WorkshopState:
    item_id: str
    descriptor_present: bool
    game_content_present: bool
    workshop_content_present: bool
    steam_install_record: bool
    manifest_mismatch: bool
    configured_active: bool | None
    url: str


class ArkEvolvedAdapter(GameAdapter):
    game = Game("ark-evolved", "ARK: Survival Evolved", ("steam",), frozenset({Platform.WINDOWS}))
    capabilities = frozenset(
        {
            GameCapability.DISCOVERY,
            GameCapability.VERSION,
            GameCapability.MOD_DISCOVERY,
            GameCapability.WORKSHOP_HANDOFF,
        }
    )

    def candidates(self, context: DiscoveryContext) -> Iterable[Path]:
        return (*context.roots, *(item.path for item in steam_installations(context, APP_ID)))

    def inspect(self, path: Path, platform: Platform) -> GameInstallation | None:
        path = path.resolve()
        if platform != Platform.WINDOWS or not (path / EXECUTABLE).is_file():
            return None
        manifest = path.parent.parent / f"appmanifest_{APP_ID}.acf"
        if not manifest.is_file() or not safe(manifest, manifest.parent):
            return None
        try:
            steam = read_appmanifest(manifest, APP_ID)
        except (OSError, ModMeshError):
            return None
        if steam.path != path:
            return None
        return GameInstallation(
            self.game.game_id,
            path,
            "steam",
            platform,
            GameVersion(build_id=steam.build_id, evidence="steam-appmanifest:346110"),
        )

    def validate(self, installation: GameInstallation) -> EnvironmentReport:
        issues = list(super().validate(installation).issues)
        inspected = self.inspect(installation.path, installation.platform)
        if inspected is None:
            issues.append(
                EnvironmentIssue("wrong_ark_edition", "Select ARK Survival Evolved Steam PC", True)
            )
        issues.append(
            EnvironmentIssue(
                "steam_managed", "Use Steam to subscribe, update and remove Workshop content"
            )
        )
        return EnvironmentReport(tuple(issues))

    def mod_locations(self, installation: GameInstallation) -> tuple[ModLocation, ...]:
        return (ModLocation("ARK Workshop mods", installation.path / MODS, "steam-workshop"),)

    def states(self, installation: GameInstallation) -> tuple[WorkshopState, ...]:
        if not self.validate(installation).valid:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid ARK Evolved installation")
        root = installation.path
        steamapps = root.parent.parent
        workshop = steamapps / "workshop/content" / APP_ID
        descriptors, game_content, cached = set(), set(), set()
        for path in children(root / MODS, root):
            if path.is_file() and path.suffix.lower() == ".mod" and numeric(path.stem):
                descriptors.add(path.stem)
            elif path.is_dir() and numeric(path.name) and children(path, root):
                game_content.add(path.name)
        for path in children(workshop, steamapps):
            if path.is_dir() and numeric(path.name) and children(path, steamapps):
                cached.add(path.name)
        installed, details = {}, {}
        acf = steamapps / "workshop" / f"appworkshop_{APP_ID}.acf"
        if acf.exists():
            if not safe(acf, steamapps):
                raise ModMeshError(ErrorCode.VALIDATION, "Unsafe Workshop index path")
            data = read_keyvalues(acf).get("appworkshop")
            if not isinstance(data, dict) or data.get("appid") != APP_ID:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Workshop app ID does not match ARK Evolved"
                )
            raw = data.get("workshopitemsinstalled", {})
            raw_details = data.get("workshopitemdetails", {})
            installed = raw if isinstance(raw, dict) else {}
            details = raw_details if isinstance(raw_details, dict) else {}
        active: set[str] | None = None
        settings = root / SETTINGS
        if settings.exists():
            if not safe(settings, root) or settings.stat().st_size > 2 * 1024**2:
                raise ModMeshError(ErrorCode.VALIDATION, "Unsafe or oversized ARK settings")
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                with settings.open(encoding="utf-8-sig") as stream:
                    parser.read_file(stream)
                if parser.has_option("ServerSettings", "ActiveMods"):
                    active = {
                        value.strip()
                        for value in parser.get("ServerSettings", "ActiveMods").split(",")
                        if numeric(value.strip())
                    }
            except (OSError, UnicodeError, configparser.Error) as exc:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Cannot read ARK active mod settings"
                ) from exc
        identifiers = (
            descriptors | game_content | cached | {value for value in installed if numeric(value)}
        )
        identifiers |= active or set()
        result = []
        for identity in sorted(identifiers, key=int):
            before, after = installed.get(identity), details.get(identity)
            mismatch = (
                isinstance(before, dict)
                and isinstance(after, dict)
                and bool(before.get("manifest"))
                and bool(after.get("manifest"))
                and before.get("manifest") != after.get("manifest")
            )
            result.append(
                WorkshopState(
                    identity,
                    identity in descriptors,
                    identity in game_content,
                    identity in cached,
                    identity in installed,
                    mismatch,
                    identity in active if active is not None else None,
                    item_url(identity),
                )
            )
        return tuple(result)

    def scan_mods(self, installation: GameInstallation) -> tuple[InstalledMod, ...]:
        return tuple(
            InstalledMod(
                installation.installation_id,
                "workshop:" + state.item_id,
                None,
                (InstalledFile(f"{MODS}/{state.item_id}.mod", False),)
                if state.descriptor_present
                else (),
                SourceRef("workshop", state.item_id),
                "Workshop " + state.item_id,
                "steam-workshop",
                state.configured_active,
            )
            for state in self.states(installation)
        )
