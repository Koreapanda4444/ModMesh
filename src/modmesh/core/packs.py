from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.planning import portable_path
from modmesh.core.resolver import ResolvedMod
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import GameInstallation, Loader, Platform
from modmesh.domain.validation import require_id
from modmesh.storage.installed import (
    array_record,
    decode_mod,
    encode_mod,
    integer_record,
    object_record,
    string_record,
)

MAX_MANIFEST_BYTES = 2 * 1024**2


def closed(value: object, keys: set[str]) -> dict[str, object]:
    data = object_record(value)
    if set(data) != keys:
        raise ModMeshError(ErrorCode.VALIDATION, "Manifest fields do not match schema 1")
    return data


def identity(value: object) -> str:
    text = string_record(value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,255}", text):
        raise ModMeshError(
            ErrorCode.VALIDATION, "Manifest identifier must be an opaque platform ID"
        )
    return text


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ModMeshError(ErrorCode.VALIDATION, "Duplicate JSON key in manifest")
        result[key] = value
    return result


@dataclass(frozen=True)
class PackContext:
    game_id: str
    edition: str
    platform: Platform
    game_version: str | None
    build_id: str | None
    loader: Loader | None

    @classmethod
    def from_installation(cls, installation: GameInstallation) -> PackContext:
        return cls(
            installation.game_id,
            installation.edition,
            installation.platform,
            installation.version.value,
            installation.version.build_id,
            installation.loader,
        )

    def document(self) -> dict[str, object]:
        return {
            "game_id": self.game_id,
            "edition": self.edition,
            "platform": self.platform.value,
            "game_version": self.game_version,
            "build_id": self.build_id,
            "loader": {"name": self.loader.name, "version": self.loader.version}
            if self.loader
            else None,
        }

    @classmethod
    def read(cls, value: object) -> PackContext:
        data = closed(
            value, {"game_id", "edition", "platform", "game_version", "build_id", "loader"}
        )
        game_id = identity(data["game_id"])
        require_id(game_id, "manifest game")
        loader = None
        if data["loader"] is not None:
            raw = closed(data["loader"], {"name", "version"})
            loader = Loader(
                identity(raw["name"]),
                identity(raw["version"]) if raw["version"] is not None else None,
            )
        return cls(
            game_id,
            identity(data["edition"]),
            Platform(string_record(data["platform"])),
            identity(data["game_version"]) if data["game_version"] is not None else None,
            identity(data["build_id"]) if data["build_id"] is not None else None,
            loader,
        )


@dataclass(frozen=True)
class PackEntry:
    mod: ResolvedMod
    rule_id: str | None = None
    prefix: str = ""

    def document(self) -> dict[str, object]:
        raw = encode_mod(self.mod)
        project = object_record(raw["project"])
        raw["project"] = {key: project[key] for key in ("reference", "game_id", "name")}
        version = object_record(raw["version"])
        version["files"] = [raw["file"]]
        raw["version"] = version
        return {"mod": raw, "rule_id": self.rule_id, "prefix": self.prefix}

    @classmethod
    def read(cls, value: object) -> PackEntry:
        data = closed(value, {"mod", "rule_id", "prefix"})
        raw = closed(data["mod"], {"project", "version", "file", "direct"})
        project = closed(raw["project"], {"reference", "game_id", "name"})
        reference = closed(project["reference"], {"source_id", "project_id"})
        identity(reference["source_id"])
        identity(reference["project_id"])
        identity(project["game_id"])
        name = string_record(project["name"])
        if len(name) > 200 or any(ord(char) < 32 for char in name):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid manifest mod name")
        version = closed(
            raw["version"],
            {
                "project",
                "version_id",
                "number",
                "game_versions",
                "loaders",
                "files",
                "channel",
                "dependencies",
                "dependencies_known",
            },
        )
        identity(version["version_id"])
        if version["number"] is not None and len(string_record(version["number"])) > 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Mod version label is too long")
        if not isinstance(raw["direct"], bool) or not isinstance(
            version["dependencies_known"], bool
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Manifest flags must be booleans")
        closed(version["project"], {"source_id", "project_id"})
        for field in ("game_versions", "loaders"):
            values = array_record(version[field])
            if len(values) > 1000 or len(values) != len(set(identity(item) for item in values)):
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid manifest compatibility set")
        files = array_record(version["files"])
        if len(files) != 1:
            raise ModMeshError(ErrorCode.VALIDATION, "Manifest entry must pin one exact file")
        for file in (raw["file"], *files):
            item = closed(file, {"project", "file_id", "filename", "size", "hashes", "primary"})
            closed(item["project"], {"source_id", "project_id"})
            identity(item["file_id"])
            if not isinstance(item["primary"], bool):
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid primary file flag")
            hashes = array_record(item["hashes"])
            if len(hashes) > 4:
                raise ModMeshError(ErrorCode.VALIDATION, "Too many file hashes")
            for entry in hashes:
                closed(entry, {"algorithm", "value"})
        dependencies = array_record(version["dependencies"])
        if len(dependencies) > 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Too many manifest dependencies")
        for dependency in dependencies:
            item = closed(dependency, {"kind", "target", "constraint", "game_versions", "loaders"})
            target = closed(
                item["target"],
                {"source_id", "project_id", "version_id", "canonical_id", "filename"},
            )
            for key, entry in target.items():
                if entry is not None:
                    if key == "filename":
                        if "/" in portable_path(string_record(entry)):
                            raise ModMeshError(
                                ErrorCode.VALIDATION, "Dependency filename must be a basename"
                            )
                    else:
                        identity(entry)
            constraint = closed(item["constraint"], {"exact_versions", "expression", "scheme"})
            if len(array_record(constraint["exact_versions"])) > 256:
                raise ModMeshError(ErrorCode.VALIDATION, "Too many exact dependency versions")
            for entry in array_record(constraint["exact_versions"]):
                identity(entry)
            if (
                constraint["expression"] is not None
                and len(string_record(constraint["expression"])) > 1024
            ):
                raise ModMeshError(ErrorCode.VALIDATION, "Dependency range is too long")
            if constraint["scheme"] is not None:
                identity(constraint["scheme"])
            for key in ("game_versions", "loaders"):
                values = array_record(item[key])
                if len(values) > 1000:
                    raise ModMeshError(ErrorCode.VALIDATION, "Too many dependency contexts")
                for entry in values:
                    identity(entry)
        prefix = data["prefix"]
        if not isinstance(prefix, str) or prefix and portable_path(prefix) != prefix:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid archive prefix")
        rule = identity(data["rule_id"]) if data["rule_id"] is not None else None
        return cls(decode_mod(raw), rule, prefix)


@dataclass(frozen=True)
class PackSetting:
    path: str
    sha256: str

    def document(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "included": False}

    @classmethod
    def read(cls, value: object) -> PackSetting:
        data = closed(value, {"path", "sha256", "included"})
        if data["included"] is not False:
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "Manifest schema 1 contains settings metadata only"
            )
        path = portable_path(string_record(data["path"]))
        if path.startswith(".modmesh-") or path.split("/")[0].casefold() in {"saves", "logs"}:
            raise ModMeshError(ErrorCode.VALIDATION, "Private data cannot be a pack setting")
        return cls(path, FileHash(HashAlgorithm.SHA256, string_record(data["sha256"])).value)


@dataclass(frozen=True)
class PackManifest:
    pack_id: str
    name: str
    revision: int
    context: PackContext
    entries: tuple[PackEntry, ...]
    settings: tuple[PackSetting, ...] = ()

    @classmethod
    def create(
        cls,
        name: str,
        context: PackContext,
        entries: tuple[PackEntry, ...],
        settings: tuple[PackSetting, ...] = (),
    ) -> PackManifest:
        value = cls(str(uuid4()), name, 1, context, entries, settings)
        return cls.read(value.document())

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pack_id": self.pack_id,
            "name": self.name,
            "revision": self.revision,
            "context": self.context.document(),
            "entries": [item.document() for item in self.entries],
            "settings": [item.document() for item in self.settings],
        }

    def dumps(self) -> bytes:
        payload = json.dumps(
            self.document(),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ModMeshError(ErrorCode.VALIDATION, "Manifest exceeds 2 MiB")
        return payload

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.dumps()).hexdigest()

    @property
    def warnings(self) -> tuple[str, ...]:
        notes = []
        if self.context.game_version is None and self.context.build_id is None:
            notes.append("Game version/build is unverified")
        for entry in self.entries:
            if not any(value.strong for value in entry.mod.file.hashes):
                notes.append(f"No strong source hash: {entry.mod.project.name}")
            if not entry.mod.version.dependencies_known:
                notes.append(f"Unverified dependencies: {entry.mod.project.name}")
        if self.settings:
            notes.append("Settings contents are excluded and will not be reproduced")
        return tuple(notes)

    @classmethod
    def read(cls, value: object) -> PackManifest:
        try:
            data = closed(
                value,
                {"schema_version", "pack_id", "name", "revision", "context", "entries", "settings"},
            )
            if type(data["schema_version"]) is not int or data["schema_version"] != 1:
                raise ModMeshError(ErrorCode.UNSUPPORTED, "Unsupported manifest schema")
            pack_id = str(UUID(string_record(data["pack_id"])))
            name = string_record(data["name"])
            if len(name) > 100 or any(ord(char) < 32 for char in name):
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Pack name needs 1–100 printable characters"
                )
            revision = integer_record(data["revision"])
            if not 1 <= revision <= 2**31 - 1:
                raise ModMeshError(ErrorCode.VALIDATION, "Invalid pack revision")
            context = PackContext.read(data["context"])
            raw = array_record(data["entries"])
            if len(raw) > 128:
                raise ModMeshError(ErrorCode.VALIDATION, "Pack entry limit is 128")
            entries = tuple(PackEntry.read(item) for item in raw)
            refs = [item.mod.project.reference for item in entries]
            if len(set(refs)) != len(refs) or any(
                item.mod.project.game_id != context.game_id for item in entries
            ):
                raise ModMeshError(ErrorCode.VALIDATION, "Duplicate or wrong-game pack entry")
            raw_settings = array_record(data["settings"])
            if len(raw_settings) > 128:
                raise ModMeshError(ErrorCode.VALIDATION, "Too many settings")
            settings = tuple(PackSetting.read(item) for item in raw_settings)
            if len({item.path.casefold() for item in settings}) != len(settings):
                raise ModMeshError(ErrorCode.VALIDATION, "Duplicate setting path")
            return cls(pack_id, name, revision, context, entries, settings)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid ModMesh manifest") from exc

    @classmethod
    def loads(cls, payload: bytes) -> PackManifest:
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ModMeshError(ErrorCode.VALIDATION, "Manifest exceeds 2 MiB")
        try:
            value = json.loads(
                payload,
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            manifest = cls.read(value)
            if len(manifest.dumps()) > MAX_MANIFEST_BYTES:
                raise ModMeshError(ErrorCode.VALIDATION, "Manifest exceeds 2 MiB")
            return manifest
        except (ValueError, TypeError, RecursionError) as exc:
            raise ModMeshError(ErrorCode.VALIDATION, "Manifest must be bounded UTF-8 JSON") from exc
