from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from uuid import uuid4

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import publish_file
from modmesh.core.installation import (
    InstallationManager,
    OperationProgress,
    OperationResult,
    _check_cancelled,
    _copy,
    _same,
    _sync_dir,
)
from modmesh.core.planning import FileSnapshot, PlanIssue, snapshot, target_path
from modmesh.domain.models import GameInstallation, SourceRef
from modmesh.storage.installed import (
    ManagedMod,
    array_record,
    mod_key,
    object_record,
    string_record,
)
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class RemovalChange:
    destination: str
    action: str
    before: FileSnapshot | None
    owners: tuple[str, ...]
    backup: str | None = None
    backup_proof: FileSnapshot | None = None


@dataclass(frozen=True)
class RemovalPlan:
    plan_id: str
    installation: GameInstallation
    root_device: int
    root_inode: int
    mods: tuple[ManagedMod, ...]
    changes: tuple[RemovalChange, ...]
    issues: tuple[PlanIssue, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        return bool(self.mods) and not self.issues

    @property
    def fingerprint(self) -> str:
        payload = {
            "root": str(self.installation.path),
            "device": self.root_device,
            "inode": self.root_inode,
            "game": self.installation.game_id,
            "mods": [(mod_key(mod.mod.project.reference), mod.operation_id) for mod in self.mods],
            "changes": [asdict(change) for change in self.changes],
            "issues": [asdict(issue) for issue in self.issues],
            "warnings": self.warnings,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class RemovalManager(InstallationManager):
    def _baseline(self, store: MetadataStore, value: str) -> tuple[str, FileSnapshot]:
        parts = value.split("/")
        if len(parts) != 3 or parts[1] != "backups" or not parts[2].isdigit():
            raise ModMeshError(ErrorCode.STORAGE, "Invalid original backup reference")
        operation = self._operation_path(parts[0])
        record = store.get("install.operations", parts[0])
        if record is None or record.get("state") != "committed":
            raise ModMeshError(ErrorCode.STORAGE, "Original backup journal is missing")
        info = operation.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (record.get("device"), record.get("inode")):
            raise ModMeshError(ErrorCode.CONFLICT, "Original backup directory changed")
        steps = array_record(record.get("steps"))
        index = int(parts[2])
        if index >= len(steps):
            raise ModMeshError(ErrorCode.STORAGE, "Invalid original backup index")
        proof = object_record(object_record(steps[index]).get("backup"))
        path = self._slot(operation, "backups", index)
        if not _same(path, proof):
            raise ModMeshError(ErrorCode.INTEGRITY, "Original backup is missing or changed")
        return value, snapshot(path)

    def _removal_plan(
        self, store: MetadataStore | None, references: Sequence[SourceRef]
    ) -> RemovalPlan:
        root = self.root.stat(follow_symlinks=False)
        mods = []
        changes = []
        issues = []
        warnings = []
        selected = {mod_key(ref) for ref in references}
        files = self._file_records(store)
        if not selected:
            issues.append(PlanIssue("empty_removal", "Select at least one installed mod"))
        if store is not None and any(
            record.get("state") not in {"committed", "rolled_back", "failed"}
            for _, record in store.list("install.operations")
        ):
            issues.append(PlanIssue("pending_recovery", "Recover incomplete operations first"))
        for key in sorted(selected):
            record = store.get("install.mods", key) if store is not None else None
            if record is None:
                issues.append(PlanIssue("not_installed", f"No managed mod record for {key}"))
                continue
            mod = ManagedMod.read(record)
            if mod_key(mod.mod.project.reference) != key:
                raise ModMeshError(ErrorCode.STORAGE, "Installed mod identity mismatch")
            mods.append(mod)
            for path in mod.paths:
                if path not in files or key not in array_record(files[path].get("owners")):
                    issues.append(
                        PlanIssue("missing_ownership", "File ownership is incomplete", path)
                    )
        if store is not None:
            for key, value in store.list("install.mods"):
                if key in selected:
                    continue
                dependent = ManagedMod.read(value)
                protected = selected.intersection(mod_key(ref) for ref in dependent.dependencies)
                if protected:
                    issues.append(
                        PlanIssue(
                            "required_by",
                            f"Required by installed mod: {dependent.mod.project.name} ({key})",
                        )
                    )
                if not dependent.mod.version.dependencies_known:
                    warnings.append(f"Unverified dependencies for: {dependent.mod.project.name}")
        for record in files.values():
            owners = tuple(string_record(owner) for owner in array_record(record["owners"]))
            if not selected.intersection(owners):
                continue
            path = string_record(record["path"])
            try:
                target = target_path(self.root, path)
                before = snapshot(target) if target.exists() else None
                action = "release"
                baseline = None
                proof = None
                if record["managed"] is True and not (set(owners) - selected):
                    if before is None:
                        action = "forget"
                        warnings.append(f"Already missing; only metadata will be removed: {path}")
                        if record.get("backup") is not None:
                            raise ModMeshError(
                                ErrorCode.CONFLICT,
                                "Missing file has an original backup; recover it explicitly",
                            )
                    else:
                        if before != FileSnapshot(
                            string_record(record["sha256"]),
                            int(str(record["size"])),
                            int(str(record["device"])),
                            int(str(record["inode"])),
                        ):
                            raise ModMeshError(
                                ErrorCode.CONFLICT, "Modified or replaced file was preserved"
                            )
                        action = "delete"
                        if record.get("backup") is not None:
                            assert store is not None
                            baseline, proof = self._baseline(store, string_record(record["backup"]))
                            if proof.sha256 != record.get("original_sha256"):
                                raise ModMeshError(
                                    ErrorCode.INTEGRITY, "Original backup identity differs"
                                )
                            action = "restore"
                if action == "release":
                    warnings.append(f"Preserve shared or externally owned file: {path}")
                changes.append(RemovalChange(path, action, before, owners, baseline, proof))
            except (OSError, ModMeshError) as exc:
                issues.append(
                    PlanIssue(
                        "preserved_file",
                        exc.message if isinstance(exc, ModMeshError) else "File inspection failed",
                        path,
                    )
                )
        adapter = self.games.get(self.installation.game_id)
        operation = adapter.validate_operation(self.installation)
        issues.extend(
            PlanIssue(issue.code, issue.message) for issue in operation.issues if issue.blocking
        )
        restored = tuple(
            self._slot(
                self._operation_path(change.backup.split("/")[0]),
                "backups",
                int(change.backup.split("/")[2]),
            )
            for change in changes
            if change.backup
        )
        native = adapter.validate_removal(
            self.installation,
            tuple(
                change.destination for change in changes if change.action in {"delete", "restore"}
            ),
            restored,
        )
        issues.extend(
            PlanIssue(issue.code, issue.message) for issue in native.issues if issue.blocking
        )
        warnings.extend(issue.message for issue in native.issues if not issue.blocking)
        return RemovalPlan(
            str(uuid4()),
            self.installation,
            root.st_dev,
            root.st_ino,
            tuple(mods),
            tuple(changes),
            tuple(issues),
            tuple(warnings),
        )

    def plan_removal(self, references: Sequence[SourceRef]) -> RemovalPlan:
        with self._locked() as store:
            return self._removal_plan(store, references)

    def remove(
        self,
        plan: RemovalPlan,
        approval: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[OperationProgress], None] | None = None,
    ) -> OperationResult:
        if approval != plan.fingerprint or not plan.applicable:
            raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact applicable removal plan")
        _check_cancelled(cancelled)
        with self._locked(create=True, cancelled=cancelled) as store:
            assert store is not None
            current = self._removal_plan(
                store, tuple(mod.mod.project.reference for mod in plan.mods)
            )
            if current.fingerprint != approval or not current.applicable:
                raise ModMeshError(ErrorCode.CONFLICT, "Removal plan changed; review it again")
            current_game = self.games.get(self.installation.game_id).inspect(
                self.root, self.installation.platform
            )
            if (
                current_game is None
                or current_game.version != self.installation.version
                or current_game.loader != self.installation.loader
            ):
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Game or loader changed after removal approval"
                )
            operation_id = plan.plan_id
            operation = self._operation_path(operation_id)
            operation.mkdir(mode=0o700)
            for directory in ("staging", "backups", "moved", "withdrawn"):
                (operation / directory).mkdir(mode=0o700)
            info = operation.stat(follow_symlinks=False)
            record: dict[str, object] = {
                "id": operation_id,
                "kind": "remove",
                "state": "preparing",
                "fingerprint": approval,
                "game_id": self.installation.game_id,
                "device": info.st_dev,
                "inode": info.st_ino,
                "root_device": plan.root_device,
                "root_inode": plan.root_inode,
                "steps": [],
                "created_dirs": [],
                "issues": [],
                "old_files": {
                    change.destination.casefold(): store.get(
                        "install.files", change.destination.casefold()
                    )
                    for change in plan.changes
                },
                "old_mods": {
                    mod_key(mod.mod.project.reference): store.get(
                        "install.mods", mod_key(mod.mod.project.reference)
                    )
                    for mod in plan.mods
                },
            }
            store.put("install.operations", operation_id, record)
            try:
                steps: list[dict[str, object]] = []
                for index, change in enumerate(plan.changes):
                    _check_cancelled(cancelled)
                    stage = None
                    backup = None
                    if change.action in {"delete", "restore"}:
                        assert change.before is not None
                        target = target_path(self.root, change.destination)
                        if snapshot(target) != change.before:
                            raise ModMeshError(
                                ErrorCode.CONFLICT, "Removal target changed before preparation"
                            )
                        with target.open("rb") as stream:
                            destination = self._slot(operation, "backups", index)
                            _copy(
                                stream,
                                destination,
                                change.before.size,
                                change.before.sha256,
                                cancelled,
                            )
                        backup = asdict(snapshot(destination))
                        if change.action == "restore":
                            assert change.backup is not None and change.backup_proof is not None
                            baseline, proof = self._baseline(store, change.backup)
                            if proof != change.backup_proof:
                                raise ModMeshError(ErrorCode.INTEGRITY, "Original backup changed")
                            parts = baseline.split("/")
                            original = self._slot(
                                self._operation_path(parts[0]), "backups", int(parts[2])
                            )
                            destination = self._slot(operation, "staging", index)
                            with original.open("rb") as stream:
                                _copy(stream, destination, proof.size, proof.sha256, cancelled)
                            stage = asdict(snapshot(destination))
                    steps.append(
                        {
                            "path": change.destination,
                            "action": change.action,
                            "state": "prepared",
                            "before": asdict(change.before) if change.before else None,
                            "stage": stage,
                            "backup": backup,
                        }
                    )
                    record["steps"] = steps
                    store.put("install.operations", operation_id, record)
                    self._emit(
                        progress,
                        operation_id,
                        "preparing",
                        index + 1,
                        len(plan.changes),
                        change.destination,
                    )
                record["state"] = "applying"
                store.put("install.operations", operation_id, record)
                for index, (change, step) in enumerate(zip(plan.changes, steps, strict=True)):
                    _check_cancelled(cancelled)
                    if change.action not in {"delete", "restore"}:
                        continue
                    target = target_path(self.root, change.destination)
                    if snapshot(target) != change.before:
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Removal target changed after approval"
                        )
                    step["state"] = "publishing"
                    record["steps"] = steps
                    store.put("install.operations", operation_id, record)
                    moved = self._slot(operation, "moved", index)
                    os.rename(target, moved)
                    _sync_dir(target.parent)
                    _sync_dir(moved.parent)
                    if snapshot(moved) != change.before:
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Removal target changed during transfer"
                        )
                    if change.action == "restore":
                        publish_file(self._slot(operation, "staging", index), target)
                        if not _same(target, object_record(step["stage"])):
                            raise ModMeshError(
                                ErrorCode.INTEGRITY, "Restored original changed before commit"
                            )
                        _sync_dir(target.parent)
                    elif target.exists():
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "A user file appeared during removal"
                        )
                    step["state"] = "applied"
                    record["steps"] = steps
                    store.put("install.operations", operation_id, record)
                    self._emit(
                        progress,
                        operation_id,
                        "applying",
                        index + 1,
                        len(steps),
                        change.destination,
                    )
                _check_cancelled(cancelled)
                selected = {mod_key(mod.mod.project.reference) for mod in plan.mods}
                with store.transaction(write=True) as transaction:
                    for change in plan.changes:
                        key = change.destination.casefold()
                        previous = transaction.get("install.files", key)
                        assert previous is not None
                        owners = set(change.owners) - selected
                        if owners:
                            previous["owners"] = sorted(owners)
                            transaction.put("install.files", key, previous)
                        else:
                            transaction.delete("install.files", key)
                    for key in selected:
                        transaction.delete("install.mods", key)
                    record["state"] = "committed"
                    transaction.put("install.operations", operation_id, record)
                self._cleanup(store, record)
                for change in plan.changes:
                    if change.backup:
                        previous_operation = store.get(
                            "install.operations", change.backup.split("/")[0]
                        )
                        if previous_operation is not None:
                            self._cleanup(store, previous_operation)
                self._emit(progress, operation_id, "committed", len(steps), len(steps))
                return OperationResult(operation_id, "committed")
            except Exception as exc:
                result = self._rollback(store, record)
                raise ModMeshError(
                    exc.code if isinstance(exc, ModMeshError) else ErrorCode.STORAGE,
                    exc.message
                    if isinstance(exc, ModMeshError)
                    else "Removal failed before commit",
                    details={
                        "operation_id": operation_id,
                        "recovery": result.state,
                        "issues": result.issues,
                    },
                ) from exc
