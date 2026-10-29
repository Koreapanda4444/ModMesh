from __future__ import annotations

import json
from dataclasses import dataclass

from modmesh.core.conflicts import analyze_dependencies, dependency_matches
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_identity import GameIdentity, capture_game_identity
from modmesh.core.games import GameAdapter
from modmesh.core.packs import MAX_MANIFEST_BYTES, PackContext, PackEntry, _pairs
from modmesh.core.provenance import ArtifactProvenance
from modmesh.core.resolver import DependencyEdge, DependencyResolution
from modmesh.core.rules import RegisteredRule
from modmesh.core.workspaces import Workspace, document_digest
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import DependencyKind
from modmesh.domain.models import GameInstallation
from modmesh.storage.installed import array_record, integer_record, object_record, string_record


@dataclass(frozen=True)
class PackLock:
    workspace: Workspace
    context: PackContext
    rule_revision: int
    rule_sha256: str
    entries: tuple[PackEntry, ...]
    warnings: tuple[str, ...]
    game_identity: GameIdentity | None = None
    provenance: tuple[ArtifactProvenance, ...] | None = None

    def document(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 2,
            "kind": "modmesh-lock",
            "workspace": self.workspace.document(),
            "context": self.context.document(),
            "rule": {"revision": self.rule_revision, "sha256": self.rule_sha256},
            "entries": [entry.document() for entry in self.entries],
            "warnings": self.warnings,
        }
        if self.game_identity is not None:
            result["game_identity"] = self.game_identity.document()
        if self.provenance is not None:
            result["provenance"] = [item.document() for item in self.provenance]
        return result

    @property
    def digest(self) -> str:
        return document_digest(self.document())

    def dumps(self) -> bytes:
        data = json.dumps(
            self.document(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(data) > MAX_MANIFEST_BYTES:
            raise ModMeshError(ErrorCode.VALIDATION, "Lockfile exceeds 2 MiB")
        return data

    @classmethod
    def read(cls, value: object) -> PackLock:
        data = object_record(value)
        if (
            set(data) - {"game_identity", "provenance"}
            != {"schema_version", "kind", "workspace", "context", "rule", "entries", "warnings"}
            or integer_record(data["schema_version"]) != 2
            or data["kind"] != "modmesh-lock"
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Unsupported lockfile schema")
        workspace = Workspace.read(data["workspace"])
        context = PackContext.read(data["context"])
        if workspace.game_id != context.game_id:
            raise ModMeshError(ErrorCode.CONFLICT, "Lockfile game context mismatch")
        rule = object_record(data["rule"])
        if (
            set(rule) != {"revision", "sha256"}
            or not 1 <= integer_record(rule["revision"]) <= 1000000
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid locked rule revision")
        rule_hash = FileHash(HashAlgorithm.SHA256, string_record(rule["sha256"])).value
        entries = tuple(PackEntry.read(item) for item in array_record(data["entries"]))
        if len(entries) != len(workspace.enabled_entries) or any(
            selected.mod != declared.mod
            or selected.prefix != declared.prefix
            or selected.rule_id is None
            or (declared.rule_id is not None and selected.rule_id != declared.rule_id)
            for selected, declared in zip(entries, workspace.enabled_entries, strict=True)
        ):
            raise ModMeshError(ErrorCode.CONFLICT, "Locked entries differ from workspace selection")
        warnings = tuple(string_record(item) for item in array_record(data["warnings"]))
        if len(warnings) > 1024 or any(
            len(item) > 1000 or any(not char.isprintable() for char in item) for item in warnings
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid lockfile warnings")
        game_identity = (
            GameIdentity.read(data["game_identity"]) if "game_identity" in data else None
        )
        if game_identity is not None and game_identity.context != context:
            raise ModMeshError(ErrorCode.CONFLICT, "Game identity and lock context differ")
        provenance = None
        if "provenance" in data:
            raw = array_record(data["provenance"])
            if len(raw) != len(entries):
                raise ModMeshError(ErrorCode.INTEGRITY, "Locked provenance count differs")
            provenance = tuple(
                ArtifactProvenance.read(item, entry)
                for item, entry in zip(raw, entries, strict=True)
            )
        return cls(
            workspace,
            context,
            integer_record(rule["revision"]),
            rule_hash,
            entries,
            warnings,
            game_identity,
            provenance,
        )

    @classmethod
    def loads(cls, data: bytes) -> PackLock:
        if len(data) > MAX_MANIFEST_BYTES:
            raise ModMeshError(ErrorCode.VALIDATION, "Lockfile exceeds 2 MiB")
        try:
            return cls.read(json.loads(data.decode("utf-8"), object_pairs_hook=_pairs))
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid lockfile JSON") from exc

    def resolution(self) -> DependencyResolution:
        mods = tuple(entry.mod for entry in self.entries)
        edges = tuple(
            DependencyEdge(parent.project.reference, target.project.reference, declaration)
            for parent in mods
            for declaration in parent.version.dependencies
            if declaration.kind == DependencyKind.REQUIRED
            for target in mods
            if dependency_matches(declaration, target)
        )
        return DependencyResolution(
            self.context.game_id,
            self.context.game_version,
            self.context.loader.name if self.context.loader else None,
            mods,
            edges,
        )


def build_lock(
    workspace: Workspace, installation: GameInstallation, adapter: GameAdapter, rule: RegisteredRule
) -> PackLock:
    if (
        workspace.game_id != installation.game_id
        or rule.rule.game_id != workspace.game_id
        or not rule.approved
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Select the matching approved game rule")
    report = adapter.validate(installation)
    if not report.valid:
        raise ModMeshError(ErrorCode.CONFLICT, "Game environment failed validation")
    entries = []
    warnings = [issue.message for issue in report.issues if not issue.blocking]
    for entry in workspace.enabled_entries:
        choices = tuple(
            item
            for item in adapter.installation_rules(installation, entry.mod.file)
            if entry.rule_id is None or item.rule_id == entry.rule_id
        )
        if len(choices) != 1:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Choose one explicit install strategy for each enabled mod"
            )
        entries.append(PackEntry(entry.mod, choices[0].rule_id, entry.prefix))
        if not any(value.strong for value in entry.mod.file.hashes):
            warnings.append("Source artifact has no strong expected hash")
        if entry.mod.project.reference.source_id != "local":
            warnings.append(
                "Remote artifact acquisition has not been verified by this lock preview"
            )
    lock = PackLock(
        workspace,
        PackContext.from_installation(installation),
        rule.rule.revision,
        rule.rule.digest,
        tuple(entries),
        (),
        capture_game_identity(installation, rule.rule),
        tuple(ArtifactProvenance(entry) for entry in entries),
    )
    conflicts = analyze_dependencies(lock.resolution())
    if conflicts.blocked:
        raise ModMeshError(
            ErrorCode.CONFLICT,
            "Enabled mod dependencies or compatibility conflict",
            details={"issues": [issue.message for issue in conflicts.issues]},
        )
    warnings += [issue.message for issue in conflicts.issues if not issue.blocking]
    return PackLock.loads(
        PackLock(
            workspace,
            lock.context,
            lock.rule_revision,
            lock.rule_sha256,
            lock.entries,
            tuple(sorted(set(warnings))),
            lock.game_identity,
            lock.provenance,
        ).dumps()
    )
