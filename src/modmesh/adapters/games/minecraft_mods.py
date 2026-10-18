from __future__ import annotations

import io
import json
import re
import stat
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from semantic_version import NpmSpec, Version

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import EnvironmentIssue, EnvironmentReport
from modmesh.core.planning import portable_path
from modmesh.domain.dependencies import DependencyKind
from modmesh.domain.models import GameInstallation

_BUILTINS = frozenset({"minecraft", "java", "fabricloader", "forge", "neoforge"})


def mod_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", value):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid native Minecraft mod identifier")
    return value


def text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\0" in value:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid native Minecraft metadata value")
    return value


@dataclass(frozen=True)
class NativeDependency:
    target: str
    kind: DependencyKind
    expressions: tuple[str, ...]
    scheme: str
    strict: bool = True


@dataclass(frozen=True)
class NativeMod:
    mod_id: str
    version: str | None
    name: str
    loader: str
    dependencies: tuple[NativeDependency, ...]
    aliases: tuple[str, ...] = ()
    environment: str = "*"
    language_range: str | None = None


@dataclass(frozen=True)
class JarMetadata:
    mods: tuple[NativeMod, ...]
    warnings: tuple[str, ...] = ()
    language_range: str | None = None
    metadata_file: str | None = None


def _read(archive: zipfile.ZipFile, name: str, limit: int = 2 * 1024**2) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ModMeshError(ErrorCode.VALIDATION, "JAR metadata exceeds its size limit")
    with archive.open(info) as stream:
        payload = stream.read(limit + 1)
    if len(payload) != info.file_size or len(payload) > limit:
        raise ModMeshError(ErrorCode.INTEGRITY, "JAR metadata size is inconsistent")
    return payload


def _object(payload: bytes) -> dict[str, object]:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ModMeshError(ErrorCode.VALIDATION, "Native mod metadata must be an object")
    return dict(data)


def _fabric(archive: zipfile.ZipFile) -> tuple[JarMetadata, tuple[str, ...]]:
    data = _object(_read(archive, "fabric.mod.json"))
    if data.get("schemaVersion") != 1:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Only Fabric metadata schema 1 is supported")
    identity, version = mod_id(data.get("id")), text(data.get("version"))
    dependencies = []
    for field, kind in (
        ("depends", DependencyKind.REQUIRED),
        ("breaks", DependencyKind.INCOMPATIBLE),
        ("conflicts", DependencyKind.INCOMPATIBLE),
        ("recommends", DependencyKind.OPTIONAL),
        ("suggests", DependencyKind.OPTIONAL),
    ):
        values = data.get(field, {})
        if not isinstance(values, dict) or len(values) > 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid native dependency map")
        for key, value in values.items():
            expressions = (
                tuple(text(item) for item in value) if isinstance(value, list) else (text(value),)
            )
            if not expressions or len(expressions) > 64:
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid native dependency predicates")
            dependencies.append(
                NativeDependency(
                    mod_id(key), kind, expressions, "fabric", field in {"depends", "breaks"}
                )
            )
    aliases = data.get("provides", [])
    if not isinstance(aliases, list) or len(aliases) > 128:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid provided mod aliases")
    environment = text(data.get("environment", "*"))
    if environment not in {"*", "client", "server"}:
        raise ModMeshError(ErrorCode.VALIDATION, "Unknown Fabric mod environment")
    mod = NativeMod(
        identity,
        version,
        text(data.get("name", identity)),
        "fabric",
        tuple(dependencies),
        tuple(mod_id(item) for item in aliases),
        environment,
    )
    jars = data.get("jars", [])
    if (
        not isinstance(jars, list)
        or len(jars) > 256
        or any(not isinstance(item, dict) for item in jars)
    ):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Fabric nested JAR list")
    return JarMetadata((mod,), metadata_file="fabric.mod.json"), tuple(
        portable_path(text(item.get("file"))) for item in jars
    )


def _forge(archive: zipfile.ZipFile, loader: str, name: str) -> JarMetadata:
    data = tomllib.loads(_read(archive, name).decode("utf-8"))
    if data.get("modLoader") not in {"javafml", "lowcodefml"}:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "This language loader is not supported")
    language = text(data.get("loaderVersion"))
    entries = data.get("mods")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Forge/NeoForge mod list")
    dependencies = data.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Forge/NeoForge dependencies")
    implementation = None
    if "META-INF/MANIFEST.MF" in archive.namelist():
        manifest = _read(archive, "META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
        manifest = manifest.replace("\r\n ", "").replace("\n ", "")
        implementation = next(
            (
                line.partition(":")[2].strip()
                for line in manifest.splitlines()
                if line.startswith("Implementation-Version:")
            ),
            None,
        )
    mods = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid Forge/NeoForge mod entry")
        identity = mod_id(entry.get("modId"))
        version: str | None = text(entry.get("version", "${file.jarVersion}"))
        if version is not None and "${" in version:
            version = implementation if version == "${file.jarVersion}" else None
        relations = dependencies.get(identity, [])
        if not isinstance(relations, list) or len(relations) > 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid Forge/NeoForge dependency list")
        values = []
        for relation in relations:
            if not isinstance(relation, dict):
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid Forge/NeoForge dependency entry")
            side = relation.get("side", "BOTH")
            if side not in {"CLIENT", "SERVER", "BOTH"}:
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid dependency side")
            if side == "SERVER":
                continue
            if loader == "forge":
                mandatory = relation.get("mandatory")
                if not isinstance(mandatory, bool):
                    raise ModMeshError(
                        ErrorCode.VALIDATION, "Forge dependency needs mandatory state"
                    )
                kind = DependencyKind.REQUIRED if mandatory else DependencyKind.OPTIONAL
            else:
                kinds = {
                    "required": DependencyKind.REQUIRED,
                    "optional": DependencyKind.OPTIONAL,
                    "incompatible": DependencyKind.INCOMPATIBLE,
                    "discouraged": DependencyKind.OPTIONAL,
                }
                raw = relation.get(
                    "type", "required" if relation.get("mandatory", True) else "optional"
                )
                if raw not in kinds:
                    raise ModMeshError(ErrorCode.VALIDATION, "Unknown NeoForge dependency kind")
                kind = kinds[raw]
            expression = relation.get("versionRange", "")
            if not isinstance(expression, str):
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid Maven dependency range")
            values.append(
                NativeDependency(mod_id(relation.get("modId")), kind, (expression,), "maven")
            )
        mods.append(
            NativeMod(
                identity,
                version,
                text(entry.get("displayName", identity)),
                loader,
                tuple(values),
                language_range=language,
            )
        )
    return JarMetadata(tuple(mods), language_range=language, metadata_file=name)


def read_jar(path: Path, loader: str | None = None) -> JarMetadata:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024**2:
        raise ModMeshError(ErrorCode.VALIDATION, "Mod JAR must be a bounded regular file")
    budget = [0, 0]

    def inspect(source: Path | BinaryIO, depth: int, required: bool) -> JarMetadata:
        budget[0] += 1
        if depth > 8 or budget[0] > 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Nested mod JAR limit reached")
        with zipfile.ZipFile(source) as archive:
            seen = set()
            total = 0
            if len(archive.infolist()) > 30000:
                raise ModMeshError(ErrorCode.VALIDATION, "Mod JAR has too many entries")
            for info in archive.infolist():
                name = portable_path(info.orig_filename.rstrip("/"))
                if (
                    name.casefold() in seen
                    or info.flag_bits & 1
                    or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise ModMeshError(
                        ErrorCode.VALIDATION, "Duplicate, encrypted or linked JAR entry"
                    )
                seen.add(name.casefold())
                total += info.file_size
                if total > 4 * 1024**3 or info.file_size > max(1, info.compress_size) * 200:
                    raise ModMeshError(ErrorCode.VALIDATION, "JAR expansion exceeds safe limits")
            names = archive.namelist()
            nested: tuple[str, ...] = ()
            if "fabric.mod.json" in names and loader in {None, "fabric"}:
                result, nested = _fabric(archive)
            elif "META-INF/neoforge.mods.toml" in names and loader in {None, "neoforge"}:
                result = _forge(archive, "neoforge", "META-INF/neoforge.mods.toml")
            elif "META-INF/mods.toml" in names and loader in {None, "forge", "neoforge"}:
                result = _forge(archive, loader or "forge", "META-INF/mods.toml")
            elif required:
                raise ModMeshError(
                    ErrorCode.UNSUPPORTED, "JAR has no metadata for the selected loader"
                )
            else:
                return JarMetadata(())
            if "META-INF/jarjar/metadata.json" in names:
                jarjar = _object(_read(archive, "META-INF/jarjar/metadata.json")).get("jars", [])
                if (
                    not isinstance(jarjar, list)
                    or len(jarjar) > 256
                    or any(not isinstance(item, dict) for item in jarjar)
                ):
                    raise ModMeshError(ErrorCode.VALIDATION, "Invalid Jar-in-Jar metadata")
                nested += tuple(portable_path(text(item.get("path"))) for item in jarjar)
            mods, warnings = list(result.mods), list(result.warnings)
            for name in dict.fromkeys(nested):
                content = _read(archive, name, 32 * 1024**2)
                budget[1] += len(content)
                if budget[1] > 256 * 1024**2:
                    raise ModMeshError(ErrorCode.VALIDATION, "Nested JAR byte limit reached")
                child = inspect(io.BytesIO(content), depth + 1, False)
                mods.extend(child.mods)
                warnings.extend(child.warnings)
            if any(
                mod.mod_id in _BUILTINS or any(alias in _BUILTINS for alias in mod.aliases)
                for mod in mods
            ):
                raise ModMeshError(
                    ErrorCode.CONFLICT, "A mod cannot impersonate a built-in game component"
                )
            return JarMetadata(
                tuple(mods), tuple(warnings), result.language_range, result.metadata_file
            )

    try:
        return inspect(path, 0, True)
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        RecursionError,
        zipfile.BadZipFile,
        NotImplementedError,
    ) as exc:
        raise ModMeshError(ErrorCode.INTEGRITY, "Cannot safely parse mod JAR metadata") from exc


def _numeric(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){0,7}", value):
        raise ModMeshError(ErrorCode.UNSUPPORTED, "This native version syntax cannot be verified")
    parts = tuple(int(part) for part in value.split("."))
    return (*parts, *(0 for _ in range(8 - len(parts))))


def matches_native(version: str, expressions: tuple[str, ...], scheme: str) -> bool:
    if scheme == "fabric":
        for expression in expressions:
            if expression in {"*", version}:
                return True
            try:
                parts = version.split(".")
                normalized = (
                    version + ".0" * (3 - len(parts))
                    if version.isdecimal() or re.fullmatch(r"\d+\.\d+", version)
                    else version
                )
                if NpmSpec(expression).match(Version(normalized)):
                    return True
            except ValueError as exc:
                raise ModMeshError(
                    ErrorCode.UNSUPPORTED, "Fabric version predicate cannot be verified"
                ) from exc
        return False
    for expression in expressions:
        if not expression:
            return True
        parts = re.findall(r"[\[(][^\[\]()]*[\])]", expression)
        if not parts or ",".join(parts) != expression:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Maven range syntax cannot be verified")
        current = _numeric(version)
        for part in parts:
            body = part[1:-1]
            if "," not in body:
                if part[0] != "[" or part[-1] != "]":
                    raise ModMeshError(ErrorCode.VALIDATION, "Invalid exact Maven range")
                if current == _numeric(body):
                    return True
                continue
            lower, upper = body.split(",", 1)
            if (
                not lower
                or (current >= _numeric(lower) if part[0] == "[" else current > _numeric(lower))
            ) and (
                not upper
                or (current <= _numeric(upper) if part[-1] == "]" else current < _numeric(upper))
            ):
                return True
    return False


def validate_native(
    installation: GameInstallation,
    jars: tuple[tuple[str, JarMetadata], ...],
    *,
    lost: frozenset[str] | None = None,
) -> EnvironmentReport:
    issues = []
    known: dict[str, tuple[str | None, str]] = {}
    for path, jar in jars:
        for mod in jar.mods:
            for identity in dict.fromkeys((mod.mod_id, *mod.aliases)):
                if identity in known and known[identity] != (mod.version, path):
                    issues.append(
                        EnvironmentIssue(
                            "native_duplicate",
                            f"Native mod ID appears in multiple JARs: {identity}",
                            True,
                        )
                    )
                known[identity] = (mod.version, path)
    loader = installation.loader
    known["minecraft"] = (installation.version.value, "game")
    if loader is not None:
        known["fabricloader" if loader.name == "fabric" else loader.name] = (
            loader.version,
            "loader",
        )
    for path, jar in jars:
        for language in dict.fromkeys(
            mod.language_range for mod in jar.mods if mod.language_range and lost is None
        ):
            if loader and loader.name == "forge" and loader.version:
                try:
                    if not matches_native(loader.version, (language,), "maven"):
                        issues.append(
                            EnvironmentIssue(
                                "language_loader_version",
                                f"Language loader range is not met: {path}",
                                True,
                            )
                        )
                except ModMeshError as exc:
                    issues.append(
                        EnvironmentIssue("language_loader_version", f"{path}: {exc.message}", True)
                    )
            else:
                issues.append(
                    EnvironmentIssue(
                        "language_loader_unverified",
                        f"FML language-loader range is not verified: {path}",
                    )
                )
        for mod in jar.mods:
            if mod.environment == "server" and lost is None:
                issues.append(
                    EnvironmentIssue(
                        "server_only",
                        f"Server-only mod cannot be installed into a client: {mod.mod_id}",
                        True,
                    )
                )
            for dep in mod.dependencies:
                if lost is not None and dep.target not in lost:
                    continue
                if dep.target == "java":
                    if lost is None:
                        issues.append(
                            EnvironmentIssue(
                                "java_unverified",
                                f"Launcher Java runtime requirement is not verified: {mod.mod_id}",
                            )
                        )
                    continue
                actual = known.get(dep.target)
                if actual is None:
                    if dep.kind == DependencyKind.REQUIRED:
                        issues.append(
                            EnvironmentIssue(
                                "native_required",
                                f"{mod.mod_id} requires missing native mod: {dep.target}",
                                True,
                            )
                        )
                    continue
                try:
                    universal = all(expression in {"*", ""} for expression in dep.expressions)
                    if actual[0] is None and not universal:
                        raise ModMeshError(
                            ErrorCode.UNSUPPORTED, "Required native version is unknown"
                        )
                    match = universal or matches_native(
                        actual[0] or "", dep.expressions, dep.scheme
                    )
                    if (
                        dep.kind in {DependencyKind.REQUIRED, DependencyKind.OPTIONAL} and not match
                    ) or (dep.kind == DependencyKind.INCOMPATIBLE and match):
                        issues.append(
                            EnvironmentIssue(
                                "native_constraint",
                                f"Native condition failed: {mod.mod_id} -> {dep.target}",
                                dep.strict,
                            )
                        )
                except ModMeshError as exc:
                    if dep.kind in {
                        DependencyKind.REQUIRED,
                        DependencyKind.INCOMPATIBLE,
                        DependencyKind.OPTIONAL,
                    }:
                        issues.append(
                            EnvironmentIssue(
                                "native_unverified", f"{mod.mod_id}: {exc.message}", dep.strict
                            )
                        )
    return EnvironmentReport(tuple(dict.fromkeys(issues)))
