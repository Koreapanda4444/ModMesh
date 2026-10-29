from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.workspace_history import WorkspaceHistory
from modmesh.core.workspaces import Workspace, document_digest
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.storage.installed import integer_record, object_record, string_record
from modmesh.storage.metadata import MetadataStore

TERMINAL_ACTIVATIONS = frozenset({"committed", "rolled_back", "aborted", "superseded"})


@dataclass(frozen=True)
class ActivationReceipt:
    operation_id: str
    target: str
    pack_id: str
    lock_digest: str
    profile_id: str
    profile_digest: str
    previous_profile_id: str
    root_device: int
    root_inode: int
    created_at: str
    schema_version: int = 2

    def document(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def read(cls, value: object) -> ActivationReceipt:
        data = object_record(value)
        if set(data) != set(cls.__dataclass_fields__) or type(data["schema_version"]) is not int:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid activation receipt")
        strings = {
            key: string_record(data[key])
            for key in cls.__dataclass_fields__
            if key not in {"schema_version", "root_device", "root_inode"}
        }
        try:
            for key in ("operation_id", "target", "pack_id", "profile_id", "previous_profile_id"):
                if str(UUID(strings[key])) != strings[key]:
                    raise ValueError
            if (
                data["schema_version"] != 2
                or len(strings["created_at"]) > 64
                or datetime.fromisoformat(strings["created_at"]).tzinfo is None
            ):
                raise ValueError
        except ValueError as exc:
            raise ModMeshError(
                ErrorCode.STORAGE, "Invalid activation identity or timestamp"
            ) from exc
        for key in ("lock_digest", "profile_digest"):
            FileHash(HashAlgorithm.SHA256, strings[key])
        device, inode = integer_record(data["root_device"]), integer_record(data["root_inode"])
        if device < 0 or inode < 1:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid activation root identity")
        return cls(
            strings["operation_id"],
            strings["target"],
            strings["pack_id"],
            strings["lock_digest"],
            strings["profile_id"],
            strings["profile_digest"],
            strings["previous_profile_id"],
            device,
            inode,
            strings["created_at"],
        )


class WorkspaceReceipts:
    def __init__(self, database: MetadataStore) -> None:
        self.database = database

    def pending(self, target: str) -> tuple[dict[str, object], ...]:
        results = []
        for key, record in self.database.list("workspaces.operations"):
            receipt = ActivationReceipt.read(record.get("receipt"))
            if key != receipt.operation_id:
                raise ModMeshError(ErrorCode.INTEGRITY, "Activation journal identity differs")
            if receipt.target == target and record.get("state") not in TERMINAL_ACTIVATIONS:
                results.append(record)
        return tuple(results)

    def begin(
        self,
        receipt: ActivationReceipt,
        previous: dict[str, object] | None,
        workspace_digest: str,
        *,
        noop: bool,
    ) -> dict[str, object]:
        receipt = ActivationReceipt.read(receipt.document())
        lock = WorkspaceHistory(self.database).get(receipt.lock_digest).lock
        if lock.workspace.pack_id != receipt.pack_id or lock.workspace.digest != workspace_digest:
            raise ModMeshError(ErrorCode.INTEGRITY, "Activation snapshot differs from workspace")
        record: dict[str, object] = {
            "receipt": receipt.document(),
            "previous": previous,
            "state": "prepared",
            "noop": noop,
            "issues": [],
        }
        with self.database.transaction(write=True) as transaction:
            current = Workspace.read(transaction.get("workspaces.items", receipt.pack_id))
            if current.digest != workspace_digest:
                raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed before activation")
            if transaction.get("workspaces.active", receipt.target) != previous:
                raise ModMeshError(ErrorCode.CONFLICT, "Active pack changed; review a new plan")
            for _, pending in transaction.list("workspaces.operations"):
                other = ActivationReceipt.read(pending.get("receipt"))
                if (
                    other.target == receipt.target
                    and pending.get("state") not in TERMINAL_ACTIVATIONS
                ):
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "Recover the pending pack activation first"
                    )
            if transaction.get("workspaces.operations", receipt.operation_id) is not None:
                raise ModMeshError(ErrorCode.CONFLICT, "Activation operation already exists")
            transaction.put("workspaces.operations", receipt.operation_id, record)
        return record

    def complete(
        self, record: dict[str, object], profile: dict[str, object] | None = None
    ) -> ActivationReceipt:
        receipt = ActivationReceipt.read(record.get("receipt"))
        lock = WorkspaceHistory(self.database).get(receipt.lock_digest).lock
        if lock.workspace.pack_id != receipt.pack_id:
            raise ModMeshError(ErrorCode.INTEGRITY, "Activation lock belongs to a different pack")
        with self.database.transaction(write=True) as transaction:
            current = transaction.get("workspaces.operations", receipt.operation_id)
            if current != record:
                raise ModMeshError(ErrorCode.CONFLICT, "Activation journal changed")
            if transaction.get("workspaces.active", receipt.target) != record.get("previous"):
                raise ModMeshError(ErrorCode.CONFLICT, "Active receipt changed during activation")
            existing = transaction.get("workspaces.receipts", receipt.operation_id)
            if existing is not None and existing != receipt.document():
                raise ModMeshError(ErrorCode.INTEGRITY, "Immutable activation receipt differs")
            transaction.put("workspaces.receipts", receipt.operation_id, receipt.document())
            if profile is not None:
                key = receipt.target + ":" + receipt.lock_digest
                sealed = transaction.get("workspaces.profile_snapshots", key)
                if sealed is not None and sealed.get("profile_digest") != receipt.profile_digest:
                    raise ModMeshError(ErrorCode.INTEGRITY, "Historical profile snapshot differs")
                if sealed is None:
                    transaction.put(
                        "workspaces.profile_snapshots",
                        key,
                        {
                            "profile": profile,
                            "digest": document_digest(profile),
                            "profile_digest": receipt.profile_digest,
                        },
                    )
            transaction.put("workspaces.active", receipt.target, receipt.document())
            transaction.put(
                "workspaces.operations", receipt.operation_id, {**record, "state": "committed"}
            )
        return receipt

    def mark(self, record: dict[str, object], state: str, issues: tuple[str, ...] = ()) -> None:
        receipt = ActivationReceipt.read(record.get("receipt"))
        with self.database.transaction(write=True) as transaction:
            current = transaction.get("workspaces.operations", receipt.operation_id)
            if current != record:
                raise ModMeshError(ErrorCode.CONFLICT, "Activation journal changed")
            transaction.put(
                "workspaces.operations",
                receipt.operation_id,
                {**record, "state": state, "issues": list(issues)},
            )
