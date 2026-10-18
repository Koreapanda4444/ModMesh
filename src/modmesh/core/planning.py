from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from modmesh.core.conflicts import analyze_dependencies
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import GameCapability, GameRegistry, InstallRule
from modmesh.core.hashing import verify_file
from modmesh.core.resolver import DependencyResolution, ResolvedMod
from modmesh.domain.artifacts import FileHash
from modmesh.domain.models import GameInstallation, SourceRef


@dataclass(frozen=True)
class ArchivePolicy:
    max_files: int = 10000
    max_bytes: int = 8 * 1024**3
    max_ratio: int = 200


@dataclass(frozen=True)
class FileSnapshot:
    sha256: str
    size: int
    device: int
    inode: int


@dataclass(frozen=True)
class InstallInput:
    mod: ResolvedMod
    path: Path
    rule_id: str | None = None
    prefix: str = ""


@dataclass(frozen=True)
class FileChange:
    project: SourceRef
    destination: str
    source: Path
    source_sha256: str
    member: str | None
    sha256: str
    size: int
    action: str
    before: FileSnapshot | None
    owned_before: bool = False
    generated: bytes | None = None


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class InstallationPlan:
    plan_id: str
    installation: GameInstallation
    root_device: int
    root_inode: int
    resolution: DependencyResolution
    inputs: tuple[InstallInput, ...]
    changes: tuple[FileChange, ...]
    issues: tuple[PlanIssue, ...]
    warnings: tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return bool(self.changes) and not self.issues

    @property
    def fingerprint(self) -> str:
        payload = {
            "root": str(self.installation.path),
            "root_identity": (self.root_device, self.root_inode),
            "game": self.installation.game_id,
            "game_version": self.installation.version.value,
            "build_id": self.installation.version.build_id,
            "loader": (self.installation.loader.name, self.installation.loader.version)
            if self.installation.loader
            else None,
            "mods": [
                (
                    mod.project.reference.source_id,
                    mod.project.reference.project_id,
                    mod.version.version_id,
                    mod.file.file_id,
                    mod.direct,
                )
                for mod in self.resolution.mods
            ],
            "edges": [
                (
                    edge.parent.source_id,
                    edge.parent.project_id,
                    edge.target.source_id,
                    edge.target.project_id,
                    asdict(edge.declaration),
                )
                for edge in self.resolution.edges
            ],
            "changes": [
                (
                    change.project.source_id,
                    change.project.project_id,
                    change.destination,
                    change.source_sha256,
                    change.sha256,
                    change.size,
                    change.action,
                    str(change.before),
                    change.owned_before,
                )
                for change in self.changes
            ],
            "issues": [(issue.code, issue.message, issue.path) for issue in self.issues],
            "warnings": self.warnings,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                default=lambda value: (
                    sorted(value) if isinstance(value, (set, frozenset)) else str(value)
                ),
            ).encode()
        ).hexdigest()


def portable_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\\", "/"))
    parts = value.split("/")
    if (
        not value
        or len(value) > 2000
        or len(parts) > 32
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            any(ord(char) < 32 or char in ':<>"|?*' for char in part) or part.endswith((" ", "."))
            for part in parts
        )
        or any(
            re.fullmatch(
                r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])",
                part.partition(".")[0].rstrip(" ."),
            )
            for part in parts
        )
    ):
        raise ModMeshError(ErrorCode.VALIDATION, "Unsafe or nonportable installation path")
    return value


def target_path(root: Path, relative: str) -> Path:
    relative = portable_path(relative)
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ModMeshError(ErrorCode.CONFLICT, "Game root is missing or indirect")
    path = root
    for index, part in enumerate(relative.split("/")):
        parent = path
        path = path / part
        if path.is_symlink() or path.is_junction():
            raise ModMeshError(ErrorCode.CONFLICT, "Indirect installation path blocked")
        if not path.exists() and parent.is_dir():
            for count, child in enumerate(parent.iterdir()):
                if count >= 20000:
                    raise ModMeshError(
                        ErrorCode.VALIDATION, "Directory collision scan limit reached"
                    )
                if unicodedata.normalize("NFC", child.name).casefold() == part.casefold():
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "Existing path has a different letter case"
                    )
        if index < len(relative.split("/")) - 1 and path.exists() and not path.is_dir():
            raise ModMeshError(ErrorCode.CONFLICT, "A parent path is not a directory")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ModMeshError(ErrorCode.VALIDATION, "Installation path escapes the game root")
    return path


def snapshot(path: Path, cancelled: Callable[[], bool] | None = None) -> FileSnapshot:
    artifact = verify_file(path, cancelled=cancelled)
    value = path.stat(follow_symlinks=False)
    return FileSnapshot(artifact.sha256.value, artifact.size, value.st_dev, value.st_ino)


class InstallationPlanner:
    def __init__(self, games: GameRegistry, *, policy: ArchivePolicy | None = None) -> None:
        self.games = games
        self.policy = policy or ArchivePolicy()
        if (
            not 1 <= self.policy.max_files <= 100000
            or self.policy.max_bytes < 1
            or self.policy.max_ratio < 1
        ):
            raise ModMeshError(ErrorCode.CONFIGURATION, "Invalid archive limits")

    def build(
        self,
        installation: GameInstallation,
        resolution: DependencyResolution,
        inputs: Sequence[InstallInput],
        *,
        managed: Mapping[str, FileHash] | None = None,
        replace_existing: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> InstallationPlan:
        adapter = self.games.get(installation.game_id)
        if GameCapability.INSTALL_RULES not in adapter.capabilities:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Game has no verified installation rules")
        if installation.game_id != resolution.game_id:
            raise ModMeshError(ErrorCode.VALIDATION, "Installation and mod game differ")
        environment = adapter.validate(installation)
        if not environment.valid:
            raise ModMeshError(ErrorCode.VALIDATION, "Game environment failed validation")
        root = installation.path
        identity = root.stat(follow_symlinks=False)
        expected_inputs = {mod.project.reference: mod for mod in resolution.mods}
        if len(inputs) != len(expected_inputs) or {
            item.mod.project.reference for item in inputs
        } != set(expected_inputs):
            raise ModMeshError(ErrorCode.VALIDATION, "Provide artifacts for every resolved mod")
        report = analyze_dependencies(resolution)
        issues = [PlanIssue(issue.code, issue.message) for issue in report.issues if issue.blocking]
        warnings = [issue.message for issue in report.issues if not issue.blocking]
        warnings += [issue.message for issue in environment.issues if not issue.blocking]
        if resolution.game_version != installation.version.value or resolution.loader != (
            installation.loader.name if installation.loader else None
        ):
            issues.append(
                PlanIssue(
                    "context_changed", "Resolution does not match the installed game/loader context"
                )
            )
        ownership = {
            portable_path(name).casefold(): value for name, value in (managed or {}).items()
        }
        changes: list[FileChange] = []
        destinations: set[str] = set()
        for item in inputs:
            if item.mod != expected_inputs[item.mod.project.reference]:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Artifact input has different resolved metadata"
                )
            rules = adapter.installation_rules(installation, item.mod.file)
            choices = tuple(
                rule for rule in rules if item.rule_id is None or rule.rule_id == item.rule_id
            )
            if len(choices) != 1:
                issues.append(
                    PlanIssue("choose_rule", "Select one explicit game installation rule")
                )
                continue
            rule = choices[0]
            artifact = verify_file(
                item.path,
                item.mod.file.hashes,
                expected_size=item.mod.file.size,
                cancelled=cancelled,
            )
            if artifact.size > self.policy.max_bytes:
                raise ModMeshError(ErrorCode.VALIDATION, "Artifact exceeds installation size limit")
            if artifact.source_verification != "strong":
                warnings.append(
                    f"{item.mod.project.name}: source verification "
                    f"is {artifact.source_verification}"
                )
            entries = self._entries(item, rule, cancelled, warnings)
            payload_report = adapter.validate_payload(
                installation, rule, tuple(entry[0] for entry in entries)
            )
            issues += [
                PlanIssue(issue.code, issue.message)
                for issue in payload_report.issues
                if issue.blocking
            ]
            warnings += [issue.message for issue in payload_report.issues if not issue.blocking]
            generated = adapter.auxiliary_files(
                installation, rule, tuple(entry[0] for entry in entries)
            )
            for name, generated_content in generated.items():
                if len(generated_content) > 65536:
                    raise ModMeshError(
                        ErrorCode.VALIDATION, "Generated installation content exceeds limit"
                    )
                entries.append(
                    (
                        portable_path(name),
                        None,
                        hashlib.sha256(generated_content).hexdigest(),
                        len(generated_content),
                        generated_content,
                    )
                )
            for name, member, sha, size, content in entries:
                relative = portable_path(f"{rule.destination}/{name}")
                key = relative.casefold()
                if key in destinations:
                    issues.append(
                        PlanIssue(
                            "duplicate_target",
                            "Multiple payload files have the same target",
                            relative,
                        )
                    )
                    continue
                destinations.add(key)
                before = None
                owned = False
                action = "add"
                try:
                    path = target_path(root, relative)
                    if path.exists():
                        if not path.is_file():
                            raise ModMeshError(ErrorCode.CONFLICT, "Target is not a regular file")
                        before = snapshot(path, cancelled)
                        owned_hash = ownership.get(key)
                        owned = owned_hash is not None
                        if owned_hash is not None and owned_hash.value != before.sha256:
                            raise ModMeshError(
                                ErrorCode.CONFLICT, "Managed target was modified by the user"
                            )
                        if before.sha256 == sha:
                            action = "reuse"
                        elif owned or replace_existing:
                            action = "replace"
                        else:
                            raise ModMeshError(
                                ErrorCode.CONFLICT,
                                "Unmanaged target requires explicit replacement approval",
                            )
                except ModMeshError as exc:
                    if exc.code == ErrorCode.CANCELLED:
                        raise
                    issues.append(PlanIssue(exc.code.value, exc.message, relative))
                    action = "blocked"
                changes.append(
                    FileChange(
                        item.mod.project.reference,
                        relative,
                        item.path,
                        artifact.sha256.value,
                        member,
                        sha,
                        size,
                        action,
                        before,
                        owned,
                        content,
                    )
                )
            verify_file(
                item.path, (artifact.sha256,), expected_size=artifact.size, cancelled=cancelled
            )
        if (
            len(changes) > self.policy.max_files
            or sum(change.size for change in changes) > self.policy.max_bytes
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Installation plan exceeds file or byte limit")
        return InstallationPlan(
            str(uuid4()),
            installation,
            identity.st_dev,
            identity.st_ino,
            resolution,
            tuple(inputs),
            tuple(changes),
            tuple(issues),
            tuple(dict.fromkeys(warnings)),
        )

    def _entries(
        self,
        item: InstallInput,
        rule: InstallRule,
        cancelled: Callable[[], bool] | None,
        warnings: list[str],
    ) -> list[tuple[str, str | None, str, int, bytes | None]]:
        if rule.layout == "file":
            if item.prefix:
                raise ModMeshError(ErrorCode.VALIDATION, "Archive prefix is invalid for a raw file")
            if item.mod.file.filename.lower().endswith(tuple(rule.extensions)):
                artifact = verify_file(item.path, cancelled=cancelled)
                return [
                    (
                        portable_path(item.mod.file.filename),
                        None,
                        artifact.sha256.value,
                        artifact.size,
                        None,
                    )
                ]
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "File extension is not permitted by the game rule"
            )
        if rule.layout != "archive":
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Unknown installation layout")
        prefix = portable_path(item.prefix) + "/" if item.prefix else ""
        entries: list[tuple[str, str | None, str, int, bytes | None]] = []
        seen: dict[str, bool] = {}
        total = 0
        try:
            with zipfile.ZipFile(item.path) as archive:
                values = archive.infolist()
                if len(values) > self.policy.max_files:
                    raise ModMeshError(ErrorCode.VALIDATION, "Archive has too many entries")
                for info in values:
                    if cancelled is not None and cancelled():
                        raise ModMeshError(ErrorCode.CANCELLED, "Installation planning cancelled")
                    name = portable_path(info.orig_filename.rstrip("/"))
                    key = name.casefold()
                    mode = info.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if info.flag_bits & 1 or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ModMeshError(
                            ErrorCode.VALIDATION, "Encrypted or linked archive entry blocked"
                        )
                    if key in seen or any(
                        not seen.get("/".join(key.split("/")[:index]), True)
                        for index in range(1, len(key.split("/")))
                    ):
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Archive has duplicate or conflicting paths"
                        )
                    seen[key] = info.is_dir()
                    if info.is_dir():
                        continue
                    if any(other.startswith(key + "/") for other in seen):
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Archive path is both file and directory"
                        )
                    total += info.file_size
                    if (
                        info.file_size < 0
                        or total > self.policy.max_bytes
                        or info.file_size > max(1, info.compress_size) * self.policy.max_ratio
                    ):
                        raise ModMeshError(
                            ErrorCode.VALIDATION, "Archive expansion exceeds safe limits"
                        )
                    if prefix and not name.startswith(prefix):
                        continue
                    relative = portable_path(name[len(prefix) :])
                    if PurePosixPath(relative).name.casefold() in rule.ignored_files:
                        warnings.append(f"Package documentation skipped: {relative}")
                        continue
                    if (
                        "*" not in rule.extensions
                        and PurePosixPath(relative).suffix.lower() not in rule.extensions
                    ):
                        raise ModMeshError(
                            ErrorCode.UNSUPPORTED,
                            "Archive contains a file outside the selected game rule",
                        )
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info) as stream:
                        while chunk := stream.read(1024 * 1024):
                            if cancelled is not None and cancelled():
                                raise ModMeshError(
                                    ErrorCode.CANCELLED, "Installation planning cancelled"
                                )
                            size += len(chunk)
                            if size > info.file_size or size > self.policy.max_bytes:
                                raise ModMeshError(
                                    ErrorCode.INTEGRITY, "Archive entry exceeded declared size"
                                )
                            digest.update(chunk)
                    if size != info.file_size:
                        raise ModMeshError(
                            ErrorCode.INTEGRITY, "Archive entry size is inconsistent"
                        )
                    entries.append((relative, info.filename, digest.hexdigest(), size, None))
        except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
            raise ModMeshError(ErrorCode.INTEGRITY, "Cannot safely read this ZIP package") from exc
        if not entries:
            raise ModMeshError(ErrorCode.VALIDATION, "Package has no installable payload")
        return entries
