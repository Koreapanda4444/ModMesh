from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.workspace_history import PackSnapshot, WorkspaceHistory
from modmesh.core.workspaces import Workspace, WorkspaceStore, document_digest, pack_name
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class BranchPlan:
    source: Workspace
    before: dict[str, object] | None
    action: str
    snapshot: str | None
    fork: Workspace | None

    @property
    def fingerprint(self) -> str:
        return document_digest(
            {
                "source": self.source.digest,
                "before": self.before,
                "action": self.action,
                "snapshot": self.snapshot,
                "fork": self.fork.document() if self.fork else None,
            }
        )

    def document(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "pack_id": self.source.pack_id,
            "action": self.action,
            "source_revision": self.source.revision,
            "snapshot": self.snapshot,
            "fork": self.fork.document() if self.fork else None,
            "game_files_changed": False,
        }


class WorkspaceBranches:
    def __init__(self, database: MetadataStore) -> None:
        self.database = database

    def plan(self, pack_id: str, expected: str, action: str, name: str | None = None) -> BranchPlan:
        source = WorkspaceStore(self.database).get(pack_id)
        if source.digest != expected or action not in {"protect", "unprotect", "fork"}:
            raise ModMeshError(ErrorCode.CONFLICT, "Branch action or workspace changed")
        before = self.database.get("workspaces.branches", pack_id)
        snapshots = [
            item
            for item in WorkspaceHistory(self.database).list(pack_id)
            if item.lock.workspace.digest == source.digest
        ]
        snapshot = snapshots[-1].lock.digest if snapshots else None
        if action in {"protect", "fork"} and snapshot is None:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Capture the current workspace snapshot before branching"
            )
        fork = None
        if action == "fork":
            selected_name = pack_name(name or source.name + " experimental")
            identity = str(
                uuid5(
                    NAMESPACE_URL,
                    "modmesh:experimental:"
                    + document_digest([pack_id, expected, snapshot, selected_name]),
                )
            )
            if (
                self.database.get("workspaces.items", identity) is not None
                or self.database.get("workspaces.archived", identity) is not None
            ):
                raise ModMeshError(
                    ErrorCode.CONFLICT, "This experimental branch name already exists"
                )
            fork = replace(source, pack_id=identity, name=selected_name, revision=1)
        return BranchPlan(source, before, action, snapshot, fork)

    def apply(self, plan: BranchPlan, approval: str) -> Workspace:
        if (
            approval != plan.fingerprint
            or self.plan(
                plan.source.pack_id,
                plan.source.digest,
                plan.action,
                plan.fork.name if plan.fork else None,
            )
            != plan
        ):
            raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact unchanged branch plan")
        source_lock = (
            WorkspaceHistory(self.database).get(plan.snapshot).lock if plan.snapshot else None
        )
        selected = plan.fork or plan.source
        now = datetime.now(UTC).isoformat()
        with self.database.transaction(write=True) as transaction:
            current = Workspace.read(transaction.get("workspaces.items", plan.source.pack_id))
            if (
                current.digest != plan.source.digest
                or transaction.get("workspaces.branches", current.pack_id) != plan.before
            ):
                raise ModMeshError(ErrorCode.CONFLICT, "Branch source changed after approval")
            if plan.fork is not None:
                if (
                    transaction.get("workspaces.items", selected.pack_id) is not None
                    or transaction.get("workspaces.archived", selected.pack_id) is not None
                ):
                    raise ModMeshError(ErrorCode.CONFLICT, "Experimental branch already exists")
                transaction.put("workspaces.items", selected.pack_id, selected.document())
                for namespace in ("workspaces.constraints",):
                    value = transaction.get(namespace, current.pack_id)
                    if value is not None:
                        transaction.put(namespace, selected.pack_id, value)
                for namespace in ("workspaces.environments", "workspaces.profiles"):
                    for key, value in transaction.list(namespace):
                        if namespace == "workspaces.environments" and key.startswith(
                            current.pack_id + ":"
                        ):
                            transaction.put(
                                namespace, selected.pack_id + key[len(current.pack_id) :], value
                            )
                        elif (
                            namespace == "workspaces.profiles"
                            and value.get("pack_id") == current.pack_id
                        ):
                            transaction.put(
                                namespace,
                                str(value["target"]) + ":" + selected.pack_id,
                                {
                                    **value,
                                    "pack_id": selected.pack_id,
                                    "workspace_digest": selected.digest,
                                },
                            )
                assert source_lock is not None
                lock = replace(source_lock, workspace=selected)
                snapshot = PackSnapshot(lock, now)
                transaction.put("workspaces.snapshots", lock.digest, snapshot.document())
                transaction.put(
                    "workspaces.revisions", selected.pack_id + ":1", {"digest": lock.digest}
                )
            record = {
                **(plan.before or {}),
                "kind": "stable" if plan.action == "protect" else "experimental",
                "updated_at": now,
                "snapshot": plan.snapshot,
                "parent_pack_id": current.pack_id
                if plan.fork
                else (plan.before or {}).get("parent_pack_id"),
            }
            transaction.put("workspaces.branches", selected.pack_id, record)
            transaction.put(
                "workspaces.branch_history",
                plan.fingerprint,
                {**plan.document(), "created_at": now},
            )
        return selected
