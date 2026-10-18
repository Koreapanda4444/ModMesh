from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.resolver import ResolvedMod
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import (
    Dependency,
    DependencyKind,
    DependencyTarget,
    VersionConstraint,
)
from modmesh.domain.models import ModFile, ModVersion, ReleaseChannel, SourceProject, SourceRef


def object_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ModMeshError(ErrorCode.STORAGE, "Invalid installed metadata object")
    return dict(value)


def string_record(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ModMeshError(ErrorCode.STORAGE, "Invalid installed metadata string")
    return value


def optional_string(value: object) -> str | None:
    return None if value is None else string_record(value)


def integer_record(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ModMeshError(ErrorCode.STORAGE, "Invalid installed metadata integer")
    return value


def array_record(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise ModMeshError(ErrorCode.STORAGE, "Invalid installed metadata list")
    return list(value)


def reference_record(value: object) -> SourceRef:
    data = object_record(value)
    return SourceRef(string_record(data.get("source_id")), string_record(data.get("project_id")))


def mod_key(reference: SourceRef) -> str:
    return json.dumps([reference.source_id, reference.project_id], separators=(",", ":"))


def encode_mod(mod: ResolvedMod) -> dict[str, object]:
    def plain(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items() if key != "download_url"}
        if isinstance(value, (set, frozenset)):
            return [plain(item) for item in sorted(value)]
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    return object_record(plain(asdict(mod)))


def _decode_mod(payload: object) -> ResolvedMod:
    data = object_record(payload)
    project_data = object_record(data.get("project"))
    reference = reference_record(project_data.get("reference"))
    project = SourceProject(
        reference,
        string_record(project_data.get("game_id")),
        string_record(project_data.get("name")),
        str(project_data.get("summary", "")),
        tuple(string_record(item) for item in array_record(project_data.get("authors", []))),
        optional_string(project_data.get("web_url")),
        str(project_data.get("description", "")),
        tuple(string_record(item) for item in array_record(project_data.get("links", []))),
    )

    def file_record(value: object) -> ModFile:
        item = object_record(value)
        hashes = tuple(
            FileHash(
                HashAlgorithm(string_record(raw.get("algorithm"))), string_record(raw.get("value"))
            )
            for raw in (object_record(raw) for raw in array_record(item.get("hashes", [])))
        )
        return ModFile(
            reference_record(item.get("project")),
            string_record(item.get("file_id")),
            string_record(item.get("filename")),
            integer_record(item["size"]) if item.get("size") is not None else None,
            hashes=hashes,
            primary=item.get("primary") is True,
        )

    version_data = object_record(data.get("version"))
    dependencies = []
    for raw in array_record(version_data.get("dependencies", [])):
        dependency = object_record(raw)
        target = object_record(dependency.get("target"))
        constraint = object_record(dependency.get("constraint"))
        dependencies.append(
            Dependency(
                DependencyKind(string_record(dependency.get("kind"))),
                DependencyTarget(
                    optional_string(target.get("source_id")),
                    optional_string(target.get("project_id")),
                    optional_string(target.get("version_id")),
                    optional_string(target.get("canonical_id")),
                    optional_string(target.get("filename")),
                ),
                VersionConstraint(
                    frozenset(
                        string_record(item)
                        for item in array_record(constraint.get("exact_versions", []))
                    ),
                    optional_string(constraint.get("expression")),
                    optional_string(constraint.get("scheme")),
                ),
                frozenset(
                    string_record(item)
                    for item in array_record(dependency.get("game_versions", []))
                ),
                frozenset(
                    string_record(item) for item in array_record(dependency.get("loaders", []))
                ),
            )
        )
    version = ModVersion(
        reference_record(version_data.get("project")),
        string_record(version_data.get("version_id")),
        optional_string(version_data.get("number")),
        frozenset(
            string_record(item) for item in array_record(version_data.get("game_versions", []))
        ),
        frozenset(string_record(item) for item in array_record(version_data.get("loaders", []))),
        tuple(file_record(item) for item in array_record(version_data.get("files"))),
        ReleaseChannel(string_record(version_data.get("channel"))),
        tuple(dependencies),
        version_data.get("dependencies_known") is True,
    )
    return ResolvedMod(project, version, file_record(data.get("file")), data.get("direct") is True)


def decode_mod(payload: object) -> ResolvedMod:
    try:
        return _decode_mod(payload)
    except (ValueError, TypeError, KeyError) as exc:
        raise ModMeshError(ErrorCode.STORAGE, "Invalid installed mod metadata") from exc


@dataclass(frozen=True)
class ManagedMod:
    mod: ResolvedMod
    paths: tuple[str, ...]
    dependencies: tuple[SourceRef, ...]
    operation_id: str

    @classmethod
    def read(cls, value: object) -> ManagedMod:
        data = object_record(value)
        return cls(
            decode_mod(data.get("mod")),
            tuple(string_record(path) for path in array_record(data.get("paths"))),
            tuple(reference_record(ref) for ref in array_record(data.get("dependencies", []))),
            string_record(data.get("operation_id")),
        )
