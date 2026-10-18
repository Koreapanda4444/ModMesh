"""Windows Steam Palworld identification; semantic versions are never guessed."""

from collections.abc import Iterable
from pathlib import Path

from modmesh.adapters.games.steam import read_appmanifest, steam_installations
from modmesh.core.diagnostics import ModMeshError
from modmesh.core.games import (
    DiscoveryContext,
    EnvironmentIssue,
    EnvironmentReport,
    GameAdapter,
    GameCapability,
)
from modmesh.domain.models import Game, GameInstallation, GameVersion, Platform

APP_ID = "1623730"
_SHIPPING = Path("Pal/Binaries/Win64/Palworld-Win64-Shipping.exe")


class PalworldAdapter(GameAdapter):
    game = Game("palworld", "Palworld", ("steam",), frozenset({Platform.WINDOWS}))
    capabilities = frozenset({GameCapability.DISCOVERY, GameCapability.VERSION})

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
        return GameInstallation(self.game.game_id, path, "steam", platform, version)

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
        return EnvironmentReport(tuple(issues))
