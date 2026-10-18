from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import DiscoveryContext

type KeyValue = str | dict[str, KeyValue]
_TOKEN = re.compile(r'\s+|//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
_LIMIT = 2 * 1024 * 1024
_logger = logging.getLogger("modmesh.steam")


def parse_keyvalues(text: str) -> dict[str, KeyValue]:
    if len(text) > _LIMIT:
        raise ModMeshError(ErrorCode.VALIDATION, "Steam metadata exceeds size limit")
    tokens: list[tuple[str, bool]] = []
    position = 0
    for match in _TOKEN.finditer(text):
        if match.start() != position:
            raise ModMeshError(ErrorCode.VALIDATION, "Malformed Steam metadata")
        position = match.end()
        token = match.group()
        if token.isspace() or token.startswith("//"):
            continue
        quoted = token.startswith('"')
        if quoted:
            token = re.sub(r'\\([\\"])', r"\1", token[1:-1])
        tokens.append((token, quoted))
        if len(tokens) > 100_000:
            raise ModMeshError(ErrorCode.VALIDATION, "Steam metadata token limit exceeded")
    if position != len(text):
        raise ModMeshError(ErrorCode.VALIDATION, "Malformed Steam metadata")
    cursor = 0

    def parse(depth: int, nested: bool) -> dict[str, KeyValue]:
        nonlocal cursor
        if depth > 32:
            raise ModMeshError(ErrorCode.VALIDATION, "Steam metadata nesting limit exceeded")
        result: dict[str, KeyValue] = {}
        while cursor < len(tokens):
            key, key_quoted = tokens[cursor]
            cursor += 1
            if key == "}" and not key_quoted and nested:
                return result
            if (key in {"{", "}"} and not key_quoted) or cursor >= len(tokens):
                raise ModMeshError(ErrorCode.VALIDATION, "Malformed Steam metadata structure")
            value, value_quoted = tokens[cursor]
            cursor += 1
            normalized = key.casefold()
            if normalized in result or (value == "}" and not value_quoted):
                raise ModMeshError(ErrorCode.VALIDATION, "Ambiguous Steam metadata key")
            result[normalized] = (
                parse(depth + 1, True) if value == "{" and not value_quoted else value
            )
        if nested:
            raise ModMeshError(ErrorCode.VALIDATION, "Unclosed Steam metadata object")
        return result

    return parse(0, False)


def read_keyvalues(path: Path) -> dict[str, KeyValue]:
    with path.open("rb") as stream:
        data = stream.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise ModMeshError(ErrorCode.VALIDATION, "Steam metadata exceeds size limit")
    try:
        return parse_keyvalues(data.decode("utf-8-sig"))
    except UnicodeError as exc:
        raise ModMeshError(ErrorCode.VALIDATION, "Steam metadata is not valid UTF-8") from exc


def _registry_roots() -> tuple[Path, ...]:
    if sys.platform != "win32":
        return ()
    import winreg

    roots: list[Path] = []
    entries = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath", winreg.KEY_WOW64_32KEY),
    )
    for hive, key, name, view in entries:
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
                if isinstance(value, str) and value:
                    roots.append(Path(value))
        except OSError:
            continue
    return tuple(roots)


def steam_libraries(context: DiscoveryContext) -> tuple[Path, ...]:
    env = {key.casefold(): value for key, value in context.environment.items()}
    roots = list(context.roots) + list(_registry_roots())
    if value := env.get("steam_path"):
        roots.append(Path(value))
    if value := env.get("programfiles(x86)"):
        roots.append(Path(value) / "Steam")
    roots.extend((context.home / ".steam/steam", context.home / ".local/share/Steam"))
    libraries: dict[str, Path] = {}
    for root in roots:
        root = root.expanduser().resolve()
        if not (root / "steamapps").is_dir():
            continue
        libraries[str(root).casefold() if sys.platform == "win32" else str(root)] = root
        for file in (root / "steamapps/libraryfolders.vdf", root / "config/libraryfolders.vdf"):
            if not file.is_file():
                continue
            try:
                document = read_keyvalues(file).get("libraryfolders")
                if not isinstance(document, dict):
                    raise ModMeshError(ErrorCode.VALIDATION, "Missing Steam libraryfolders object")
                for key, entry in document.items():
                    if not key.isdecimal():
                        continue
                    library_value = entry.get("path") if isinstance(entry, dict) else entry
                    if isinstance(library_value, str):
                        path = Path(library_value).expanduser()
                        if path.is_absolute() and (path / "steamapps").is_dir():
                            path = path.resolve()
                            libraries[
                                str(path).casefold() if sys.platform == "win32" else str(path)
                            ] = path
            except (OSError, ModMeshError) as exc:
                _logger.warning(
                    "Cannot read a Steam library index", extra={"context": {"error": str(exc)}}
                )
    return tuple(libraries[key] for key in sorted(libraries))


@dataclass(frozen=True)
class SteamInstallation:
    app_id: str
    path: Path
    manifest: Path
    build_id: str | None
    state_flags: str | None


def read_appmanifest(manifest: Path, app_id: str) -> SteamInstallation:
    if not app_id.isdecimal() or len(app_id) > 12:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Steam application ID")
    state = read_keyvalues(manifest).get("appstate")
    if not isinstance(state, dict) or state.get("appid") != app_id:
        raise ModMeshError(ErrorCode.VALIDATION, "Steam app manifest ID does not match")
    directory = state.get("installdir")
    if (
        not isinstance(directory, str)
        or not directory
        or "/" in directory
        or "\\" in directory
        or directory in {".", ".."}
        or PureWindowsPath(directory).drive
    ):
        raise ModMeshError(ErrorCode.VALIDATION, "Unsafe Steam installation directory")
    common = (manifest.parent / "common").resolve()
    path = (common / directory).resolve()
    if not path.is_relative_to(common):
        raise ModMeshError(ErrorCode.VALIDATION, "Steam install path escapes its library")
    build = state.get("buildid")
    flags = state.get("stateflags")
    return SteamInstallation(
        app_id,
        path,
        manifest,
        build if isinstance(build, str) and build.isdecimal() else None,
        flags if isinstance(flags, str) else None,
    )


def steam_installations(context: DiscoveryContext, app_id: str) -> tuple[SteamInstallation, ...]:
    if not app_id.isdecimal() or len(app_id) > 12:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Steam application ID")
    installations: list[SteamInstallation] = []
    for library in steam_libraries(context):
        manifest = library / "steamapps" / f"appmanifest_{app_id}.acf"
        if not manifest.is_file():
            continue
        try:
            installation = read_appmanifest(manifest, app_id)
            if installation.path.is_dir():
                installations.append(installation)
        except (OSError, ModMeshError) as exc:
            _logger.warning(
                "Cannot read a Steam app manifest", extra={"context": {"error": str(exc)}}
            )
    return tuple(installations)
