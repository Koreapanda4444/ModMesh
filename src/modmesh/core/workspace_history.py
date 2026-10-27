from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.lockfiles import PackLock
from modmesh.core.workspaces import Workspace
from modmesh.storage.installed import object_record, string_record
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class PackSnapshot:
    lock: PackLock
    created_at: str

    def document(self) -> dict[str, object]:
        return {
            "lock": self.lock.document(),
            "digest": self.lock.digest,
            "created_at": self.created_at,
        }

    @classmethod
    def read(cls, value: object) -> PackSnapshot:
        data = object_record(value)
        if set(data) != {"lock", "digest", "created_at"}:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid workspace snapshot record")
        lock = PackLock.read(data["lock"])
        if lock.digest != string_record(data["digest"]):
            raise ModMeshError(ErrorCode.INTEGRITY, "Workspace snapshot was modified")
        created = string_record(data["created_at"])
        try:
            if len(created) > 64 or datetime.fromisoformat(created).tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid snapshot timestamp") from exc
        return cls(lock, created)


class WorkspaceHistory:
    def __init__(self, database: MetadataStore) -> None:
        self.database = database

    def get(self, digest: str) -> PackSnapshot:
        data = self.database.get("workspaces.snapshots", digest)
        if data is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Workspace snapshot does not exist")
        snapshot = PackSnapshot.read(data)
        if snapshot.lock.digest != digest:
            raise ModMeshError(ErrorCode.INTEGRITY, "Snapshot key and content differ")
        return snapshot

    def list(self, pack_id: str) -> tuple[PackSnapshot, ...]:
        snapshots = tuple(
            PackSnapshot.read(value) for _, value in self.database.list("workspaces.snapshots")
        )
        return tuple(
            sorted(
                (item for item in snapshots if item.lock.workspace.pack_id == pack_id),
                key=lambda item: item.lock.workspace.revision,
            )
        )

    def capture(self, lock: PackLock) -> PackSnapshot:
        lock = PackLock.loads(lock.dumps())
        key = f"{lock.workspace.pack_id}:{lock.workspace.revision}"
        with self.database.transaction(write=True) as transaction:
            current = Workspace.read(transaction.get("workspaces.items", lock.workspace.pack_id))
            if current.digest != lock.workspace.digest:
                raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed before snapshot capture")
            revision = transaction.get("workspaces.revisions", key)
            if revision is not None and revision.get("digest") != lock.digest:
                raise ModMeshError(
                    ErrorCode.CONFLICT,
                    "Snapshot revisions are immutable; create a new workspace revision",
                )
            existing = transaction.get("workspaces.snapshots", lock.digest)
            if existing is not None:
                snapshot = PackSnapshot.read(existing)
                if snapshot.lock.digest != lock.digest:
                    raise ModMeshError(ErrorCode.INTEGRITY, "Existing snapshot key differs")
                return snapshot
            snapshot = PackSnapshot(lock, datetime.now(UTC).isoformat())
            transaction.put("workspaces.snapshots", lock.digest, snapshot.document())
            transaction.put("workspaces.revisions", key, {"digest": lock.digest})
            return snapshot
