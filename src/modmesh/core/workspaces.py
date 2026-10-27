from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.packs import PackEntry
from modmesh.domain.validation import require_id
from modmesh.domain.workspaces import PackIdentity
from modmesh.storage.installed import array_record, integer_record, object_record, string_record
from modmesh.storage.metadata import MetadataStore


def document_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def pack_name(value: str) -> str:
    if not value.strip() or len(value) > 100 or any(not char.isprintable() for char in value):
        raise ModMeshError(ErrorCode.VALIDATION, "Pack name needs 1–100 printable characters")
    return value.strip()


@dataclass(frozen=True)
class Workspace:
    pack_id: str
    game_id: str
    name: str
    revision: int = 1
    entries: tuple[PackEntry, ...] = ()

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "pack_id": self.pack_id,
            "game_id": self.game_id,
            "name": self.name,
            "revision": self.revision,
            "entries": [entry.document() for entry in self.entries],
        }

    @property
    def digest(self) -> str:
        return document_digest(self.document())

    @classmethod
    def read(cls, value: object) -> Workspace:
        data = object_record(value)
        if (
            set(data) != {"schema_version", "pack_id", "game_id", "name", "revision", "entries"}
            or data["schema_version"] != 2
        ):
            raise ModMeshError(ErrorCode.STORAGE, "Unsupported workspace schema; original retained")
        identity = PackIdentity(string_record(data["pack_id"]), string_record(data["game_id"]))
        revision = integer_record(data["revision"])
        if not 1 <= revision <= 1000000:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid workspace revision")
        entries = tuple(PackEntry.read(item) for item in array_record(data["entries"]))
        if len(entries) > 128 or len({entry.mod.project.reference for entry in entries}) != len(
            entries
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Workspace needs at most 128 distinct mods")
        if any(entry.mod.project.game_id != identity.game_id for entry in entries):
            raise ModMeshError(ErrorCode.CONFLICT, "Workspace entries belong to another game")
        return cls(
            identity.pack_id,
            identity.game_id,
            pack_name(string_record(data["name"])),
            revision,
            entries,
        )


@dataclass(frozen=True)
class WorkspaceDeletion:
    workspace: Workspace
    active_targets: tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return not self.active_targets

    @property
    def fingerprint(self) -> str:
        return document_digest(
            {"archive_workspace": self.workspace.digest, "active_targets": self.active_targets}
        )


class WorkspaceStore:
    def __init__(self, database: MetadataStore) -> None:
        self.database = database

    def get(self, pack_id: str) -> Workspace:
        record = self.database.get("workspaces.items", pack_id)
        if record is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Pack workspace does not exist")
        workspace = Workspace.read(record)
        if workspace.pack_id != pack_id:
            raise ModMeshError(ErrorCode.STORAGE, "Workspace identity mismatch")
        return workspace

    def list(self, game_id: str | None = None) -> tuple[Workspace, ...]:
        if game_id is not None:
            require_id(game_id, "game ID")
        entries = tuple(
            Workspace.read(record) for _, record in self.database.list("workspaces.items")
        )
        return tuple(item for item in entries if game_id is None or item.game_id == game_id)

    def create(self, game_id: str, name: str) -> Workspace:
        identity = PackIdentity.create(game_id)
        workspace = Workspace(identity.pack_id, game_id, pack_name(name))
        self.database.put("workspaces.items", workspace.pack_id, workspace.document())
        return workspace

    def save(self, workspace: Workspace, expected: str) -> Workspace:
        workspace = Workspace.read(workspace.document())
        with self.database.transaction(write=True) as transaction:
            record = transaction.get("workspaces.items", workspace.pack_id)
            current = Workspace.read(record)
            if (
                current.digest != expected
                or workspace.revision != current.revision + 1
                or workspace.game_id != current.game_id
            ):
                raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed; refresh before editing")
            transaction.put("workspaces.items", workspace.pack_id, workspace.document())
        return workspace

    def rename(self, pack_id: str, name: str, expected: str) -> Workspace:
        current = self.get(pack_id)
        return self.save(
            replace(current, name=pack_name(name), revision=current.revision + 1), expected
        )

    def clone(self, pack_id: str, name: str, expected: str) -> Workspace:
        with self.database.transaction(write=True) as transaction:
            current = Workspace.read(transaction.get("workspaces.items", pack_id))
            if current.digest != expected:
                raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed before cloning")
            identity = PackIdentity.create(current.game_id)
            clone = replace(current, pack_id=identity.pack_id, name=pack_name(name), revision=1)
            transaction.put("workspaces.items", clone.pack_id, clone.document())
        return clone

    def deletion_plan(self, pack_id: str) -> WorkspaceDeletion:
        return WorkspaceDeletion(
            self.get(pack_id),
            tuple(
                key
                for key, receipt in self.database.list("workspaces.active")
                if receipt.get("pack_id") == pack_id
            ),
        )

    def delete(self, pack_id: str, approval: str) -> None:
        with self.database.transaction(write=True) as transaction:
            workspace = Workspace.read(transaction.get("workspaces.items", pack_id))
            plan = WorkspaceDeletion(
                workspace,
                tuple(
                    key
                    for key, receipt in transaction.list("workspaces.active")
                    if receipt.get("pack_id") == pack_id
                ),
            )
            if not plan.applicable or plan.fingerprint != approval:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Approve an inactive workspace deletion plan"
                )
            transaction.put("workspaces.archived", pack_id, workspace.document())
            transaction.delete("workspaces.items", pack_id)
