from dataclasses import dataclass
from pathlib import Path

from modmesh.adapters.games.minecraft_mods import JarMetadata, read_jar, validate_native
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import LoaderKind, LoaderRule
from modmesh.core.games import EnvironmentIssue, EnvironmentReport
from modmesh.core.loaders import LoaderStrategyRegistry
from modmesh.core.planning import target_path
from modmesh.domain.models import GameInstallation, Loader, ModFile


def ue4ss_payload(paths: tuple[str, ...]) -> EnvironmentReport:
    folded = {path.casefold() for path in paths}
    issues = [
        EnvironmentIssue(
            "ue4ss_order", "enabled.txt activation does not configure UE4SS load ordering"
        )
    ]
    for root in {path.split("/")[0] for path in folded}:
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
    return EnvironmentReport(tuple(issues))


def ue4ss_auxiliary(paths: tuple[str, ...]) -> dict[str, bytes]:
    folded = {path.casefold() for path in paths}
    return {
        root + "/enabled.txt": b""
        for root in {path.split("/")[0] for path in paths}
        if root.casefold() + "/enabled.txt" not in folded
    }


@dataclass(frozen=True)
class MarkerLoader:
    kind: LoaderKind

    def detect(self, root: Path, definition: LoaderRule) -> Loader | None:
        if self.kind == LoaderKind.NONE:
            return None
        return (
            Loader(self.kind.value)
            if any(target_path(root, name).is_file() for name in definition.markers)
            else None
        )

    def payload(self, paths: tuple[str, ...]) -> EnvironmentReport:
        if self.kind == LoaderKind.UE4SS:
            return ue4ss_payload(paths)
        if self.kind == LoaderKind.BEPINEX and not any(
            path.lower().endswith(".dll") for path in paths
        ):
            return EnvironmentReport(
                (
                    EnvironmentIssue(
                        "plugin_dll_missing", "BepInEx plugin payload needs a DLL", True
                    ),
                )
            )
        return EnvironmentReport()

    def auxiliary(self, paths: tuple[str, ...]) -> dict[str, bytes]:
        return ue4ss_auxiliary(paths) if self.kind == LoaderKind.UE4SS else {}

    def inspect(
        self, installation: GameInstallation, files: tuple[tuple[ModFile, Path], ...]
    ) -> EnvironmentReport:
        issues = []
        if self.kind == LoaderKind.BEPINEX:
            for file, path in files:
                if file.filename.lower().endswith(".dll"):
                    try:
                        with path.open("rb") as stream:
                            header = stream.read(64)
                            if len(header) != 64 or header[:2] != b"MZ":
                                raise ValueError
                            offset = int.from_bytes(header[60:64], "little")
                            if not 64 <= offset <= 1024 * 1024:
                                raise ValueError
                            stream.seek(offset)
                            if stream.read(4) != b"PE\x00\x00":
                                raise ValueError
                    except (OSError, ValueError):
                        issues.append(
                            EnvironmentIssue(
                                "invalid_plugin_binary", "DLL has no bounded PE header", True
                            )
                        )
            issues.append(
                EnvironmentIssue(
                    "plugin_compatibility_unverified",
                    "DLL/container structure does not verify BepInEx API compatibility "
                    "or game loading",
                )
            )
        return EnvironmentReport(tuple(issues))


class MinecraftArtifactInspector:
    def inspect(
        self,
        installation: GameInstallation,
        files: tuple[tuple[ModFile, Path], ...],
        *,
        existing: tuple[tuple[str, JarMetadata], ...] = (),
    ) -> EnvironmentReport:
        loader = installation.loader
        if loader is None:
            return EnvironmentReport(
                (EnvironmentIssue("missing_loader", "A supported loader must be identified", True),)
            )
        selected = []
        issues = []
        for file, path in files:
            try:
                metadata = read_jar(path, loader.name)
                if (
                    loader.name == "neoforge"
                    and metadata.metadata_file != "META-INF/neoforge.mods.toml"
                    and not (loader.version or "").startswith("20.4.")
                ):
                    raise ModMeshError(
                        ErrorCode.UNSUPPORTED, "This NeoForge version requires neoforge.mods.toml"
                    )
                selected.append(("mods/" + file.filename, metadata))
            except ModMeshError as exc:
                issues.append(
                    EnvironmentIssue("invalid_mod_jar", f"{file.filename}: {exc.message}", True)
                )
        return EnvironmentReport(
            (*issues, *validate_native(installation, (*existing, *selected)).issues)
        )


def standard_loaders() -> LoaderStrategyRegistry:
    registry = LoaderStrategyRegistry()
    for kind in (LoaderKind.NONE, LoaderKind.UE4SS, LoaderKind.BEPINEX):
        registry.register(MarkerLoader(kind))
    return registry
