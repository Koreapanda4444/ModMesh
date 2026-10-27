from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from modmesh.core.cache import _Lease
from modmesh.core.conflicts import analyze_dependencies
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import publish_file
from modmesh.core.games import GameRegistry
from modmesh.core.hashing import verify_file
from modmesh.core.planning import (
    FileSnapshot,
    InstallationPlan,
    InstallationPlanner,
    InstallInput,
    PlanIssue,
    portable_path,
    snapshot,
    target_path,
)
from modmesh.core.resolver import DependencyResolution
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import DependencyKind
from modmesh.domain.models import GameInstallation
from modmesh.storage.installed import (
    ManagedMod,
    array_record,
    encode_mod,
    integer_record,
    mod_key,
    object_record,
    string_record,
)
from modmesh.storage.metadata import MetadataStore

_STATE = ".modmesh-state-v1"
_OWNER = b"ModMesh installation state v1\n"
_LOG = logging.getLogger("modmesh.installation")
_TERMINAL = {"committed", "rolled_back", "failed"}


@dataclass(frozen=True)
class OperationProgress:
    operation_id: str
    state: str
    completed: int
    total: int
    path: str | None = None


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    state: str
    issues: tuple[str, ...] = ()


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ModMeshError(ErrorCode.CANCELLED, "Installation operation cancelled")


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


def _copy(
    stream: Readable,
    destination: Path,
    size: int,
    sha: str,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    digest = hashlib.sha256()
    count = 0
    with destination.open("xb") as output:
        while chunk := stream.read(1024 * 1024):
            _check_cancelled(cancelled)
            count += len(chunk)
            if count > size or output.write(chunk) != len(chunk):
                raise ModMeshError(ErrorCode.INTEGRITY, "Prepared file exceeded its approved size")
            digest.update(chunk)
        output.flush()
        os.fsync(output.fileno())
    if count != size or digest.hexdigest() != sha:
        raise ModMeshError(ErrorCode.INTEGRITY, "Prepared file differs from the approved content")


def _same(path: Path, proof: dict[str, object]) -> bool:
    try:
        current = snapshot(path)
        return current == FileSnapshot(
            string_record(proof.get("sha256")),
            integer_record(proof.get("size")),
            integer_record(proof.get("device")),
            integer_record(proof.get("inode")),
        )
    except (OSError, ModMeshError):
        return False


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        with suppress(OSError):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class InstallationManager:
    def __init__(self, games: GameRegistry, installation: GameInstallation) -> None:
        self.games = games
        self.installation = installation
        self.root = installation.path
        self.state = self.root / _STATE
        self.planner = InstallationPlanner(games)

    def _layout(self) -> None:
        if self.root.is_symlink() or self.root.is_junction() or not self.root.is_dir():
            raise ModMeshError(ErrorCode.CONFLICT, "Game root changed or became indirect")
        if self.state.is_symlink() or self.state.is_junction() or not self.state.is_dir():
            raise ModMeshError(ErrorCode.STORAGE, "Installation state is not a real directory")
        marker = self.state / "owner"
        if marker.is_symlink() or not marker.is_file():
            raise ModMeshError(ErrorCode.STORAGE, "Installation ownership marker is missing")
        with marker.open("rb") as stream:
            if stream.read(len(_OWNER) + 1) != _OWNER:
                raise ModMeshError(ErrorCode.STORAGE, "Unknown installation ownership marker")
        for name in (
            "operations",
            "lock",
            "index.sqlite3",
            "index.sqlite3-wal",
            "index.sqlite3-shm",
            "index.sqlite3-journal",
        ):
            path = self.state / name
            if path.is_symlink() or path.is_junction():
                raise ModMeshError(ErrorCode.STORAGE, "Indirect installation control path blocked")
            if path.exists() and not (path.is_dir() if name == "operations" else path.is_file()):
                raise ModMeshError(ErrorCode.STORAGE, "Invalid installation control path")

    @contextmanager
    def _locked(
        self, *, create: bool = False, cancelled: Callable[[], bool] | None = None
    ) -> Iterator[MetadataStore | None]:
        lease: _Lease | None = None
        store: MetadataStore | None = None
        try:
            if not self.state.exists() and not self.state.is_symlink() and not create:
                yield None
                return
            if create:
                self.state.mkdir(exist_ok=True, mode=0o700)
                marker = self.state / "owner"
                if not marker.exists():
                    if any(self.state.iterdir()):
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Unowned installation state directory was preserved"
                        )
                    with marker.open("xb") as stream:
                        stream.write(_OWNER)
                        stream.flush()
                        os.fsync(stream.fileno())
            self._layout()
            lease = _Lease(self.state / "lock")
            deadline = time.monotonic() + 30
            while not lease.acquire():
                _check_cancelled(cancelled)
                if time.monotonic() >= deadline:
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "Another installation operation is active"
                    )
                time.sleep(0.05)
            self._layout()
            if not (self.state / "index.sqlite3").exists() and not create:
                yield None
                return
            (self.state / "operations").mkdir(exist_ok=True, mode=0o700)
            store = MetadataStore(self.state / "index.sqlite3")
            yield store
        except OSError as exc:
            raise ModMeshError(
                ErrorCode.STORAGE, "Installation filesystem operation failed"
            ) from exc
        finally:
            if store is not None:
                store.close()
            if lease is not None:
                lease.close()

    def _file_records(self, store: MetadataStore | None) -> dict[str, dict[str, object]]:
        records = dict(store.list("install.files")) if store else {}
        for key, record in records.items():
            path = portable_path(string_record(record.get("path")))
            if key != path.casefold() or not isinstance(record.get("managed"), bool):
                raise ModMeshError(ErrorCode.STORAGE, "Invalid file ownership record")
            FileHash(HashAlgorithm.SHA256, string_record(record.get("sha256")))
            for name in ("device", "inode", "size"):
                integer_record(record.get(name))
            for owner in array_record(record.get("owners")):
                string_record(owner)
        return records

    def _plan(
        self,
        store: MetadataStore | None,
        resolution: DependencyResolution,
        inputs: Sequence[InstallInput],
        replace_existing: bool,
        cancelled: Callable[[], bool] | None,
    ) -> InstallationPlan:
        files = self._file_records(store)
        managed = {
            string_record(record["path"]): FileHash(
                HashAlgorithm.SHA256, string_record(record["sha256"])
            )
            for record in files.values()
            if record["managed"] is True
        }
        plan = self.planner.build(
            self.installation,
            resolution,
            inputs,
            managed=managed,
            replace_existing=replace_existing,
            cancelled=cancelled,
        )
        issues = list(plan.issues)
        installing = {mod_key(mod.project.reference) for mod in resolution.mods}
        if store is not None:
            if any(
                record.get("state") not in _TERMINAL
                for _, record in store.list("install.operations")
            ):
                issues.append(
                    PlanIssue(
                        "pending_recovery",
                        "Recover incomplete operations before applying another plan",
                    )
                )
            destinations = {change.destination.casefold() for change in plan.changes}
            for key in installing:
                record = store.get("install.mods", key)
                if record is not None and set(ManagedMod.read(record).paths) - destinations:
                    issues.append(
                        PlanIssue(
                            "obsolete_files",
                            "This update needs a removal plan for previous file paths",
                        )
                    )
        if store is not None:
            remaining = tuple(
                ManagedMod.read(value).mod
                for key, value in store.list("install.mods")
                if key not in installing
            )
            for conflict in analyze_dependencies(resolution, remaining).issues:
                if conflict.blocking:
                    issues.append(PlanIssue(conflict.code, conflict.message))
        for change in plan.changes:
            if change.destination.split("/")[0].casefold() == _STATE:
                issues.append(
                    PlanIssue(
                        "reserved_path",
                        "Mod payload overlaps installation state",
                        change.destination,
                    )
                )
            record = files.get(change.destination.casefold())
            if record is None:
                continue
            if (
                record["managed"] is True
                and change.before is not None
                and (change.before.device, change.before.inode)
                != (record["device"], record["inode"])
            ):
                issues.append(
                    PlanIssue(
                        "replaced_file",
                        "Managed file was replaced outside ModMesh",
                        change.destination,
                    )
                )
            others = (
                set(string_record(owner) for owner in array_record(record["owners"])) - installing
            )
            if others and change.sha256 != record["sha256"]:
                issues.append(
                    PlanIssue(
                        "shared_target",
                        "Another installed mod owns this target",
                        change.destination,
                    )
                )
        return replace(plan, issues=tuple(issues))

    def plan(
        self,
        resolution: DependencyResolution,
        inputs: Sequence[InstallInput],
        *,
        replace_existing: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> InstallationPlan:
        with self._locked(cancelled=cancelled) as store:
            return self._plan(store, resolution, inputs, replace_existing, cancelled)

    def installed(self) -> tuple[ManagedMod, ...]:
        with self._locked() as store:
            if store is None:
                return ()
            result = []
            for key, record in store.list("install.mods"):
                mod = ManagedMod.read(record)
                if key != mod_key(mod.mod.project.reference):
                    raise ModMeshError(ErrorCode.STORAGE, "Installed mod identity mismatch")
                result.append(mod)
            return tuple(result)

    def history(self) -> tuple[OperationResult, ...]:
        with self._locked() as store:
            if store is None:
                return ()
            return tuple(
                OperationResult(
                    key,
                    string_record(record.get("state")),
                    tuple(string_record(value) for value in array_record(record.get("issues", []))),
                )
                for key, record in store.list("install.operations")
            )

    def _operation_path(self, operation_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", operation_id):
            raise ModMeshError(ErrorCode.STORAGE, "Invalid operation identifier")
        path = self.state / "operations" / operation_id
        if path.is_symlink() or path.is_junction():
            raise ModMeshError(ErrorCode.STORAGE, "Indirect operation directory blocked")
        return path

    def _slot(self, operation: Path, directory: str, index: int) -> Path:
        if directory not in {"staging", "backups", "moved", "withdrawn"} or index < 0:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid operation file slot")
        parent = operation / directory
        if parent.is_symlink() or parent.is_junction() or not parent.is_dir():
            raise ModMeshError(ErrorCode.STORAGE, "Operation file directory changed")
        path = parent / str(index)
        if path.is_symlink() or path.is_junction():
            raise ModMeshError(ErrorCode.STORAGE, "Indirect operation file blocked")
        return path

    def _emit(
        self,
        callback: Callable[[OperationProgress], None] | None,
        operation_id: str,
        state: str,
        completed: int,
        total: int,
        path: str | None = None,
    ) -> None:
        if callback is not None:
            try:
                callback(OperationProgress(operation_id, state, completed, total, path))
            except Exception:
                _LOG.warning("Operation progress callback failed")

    def _parents(self, relative: str, record: dict[str, object], store: MetadataStore) -> None:
        parts = portable_path(relative).split("/")
        for index in range(1, len(parts)):
            name = "/".join(parts[:index])
            path = target_path(self.root, name)
            if not path.exists():
                path.mkdir()
                identity = path.stat(follow_symlinks=False)
                directories = array_record(record.get("created_dirs", []))
                directories.append(
                    {"path": name, "device": identity.st_dev, "inode": identity.st_ino}
                )
                record["created_dirs"] = directories
                store.put("install.operations", string_record(record["id"]), record)
            target_path(self.root, name)

    def apply(
        self,
        plan: InstallationPlan,
        approval: str,
        *,
        replace_existing: bool = False,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[OperationProgress], None] | None = None,
    ) -> OperationResult:
        if approval != plan.fingerprint or not plan.applicable:
            raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact applicable installation plan")
        _check_cancelled(cancelled)
        with self._locked(create=True, cancelled=cancelled) as store:
            assert store is not None
            root = self.root.stat(follow_symlinks=False)
            if (root.st_dev, root.st_ino) != (plan.root_device, plan.root_inode):
                raise ModMeshError(ErrorCode.CONFLICT, "Game root changed after plan approval")
            current_game = self.games.get(self.installation.game_id).inspect(
                self.root, self.installation.platform
            )
            if (
                current_game is None
                or current_game.version != self.installation.version
                or current_game.loader != self.installation.loader
            ):
                raise ModMeshError(ErrorCode.CONFLICT, "Game or loader changed after plan approval")
            current = self._plan(store, plan.resolution, plan.inputs, replace_existing, cancelled)
            if current.fingerprint != approval or not current.applicable:
                raise ModMeshError(ErrorCode.CONFLICT, "Installation plan changed; review it again")
            operation_id = plan.plan_id
            operation = self._operation_path(operation_id)
            operation.mkdir(mode=0o700)
            for name in ("staging", "backups", "moved", "withdrawn"):
                (operation / name).mkdir(mode=0o700)
            identity = operation.stat(follow_symlinks=False)
            keys = {change.destination.casefold() for change in plan.changes}
            mod_keys = {mod_key(mod.project.reference) for mod in plan.resolution.mods}
            old_files = {key: store.get("install.files", key) for key in keys}
            old_mods = {key: store.get("install.mods", key) for key in mod_keys}
            record: dict[str, object] = {
                "id": operation_id,
                "kind": "install",
                "state": "preparing",
                "fingerprint": approval,
                "game_id": self.installation.game_id,
                "device": identity.st_dev,
                "inode": identity.st_ino,
                "root_device": root.st_dev,
                "root_inode": root.st_ino,
                "steps": [],
                "old_files": old_files,
                "old_mods": old_mods,
                "created_dirs": [],
                "created_at": datetime.now(UTC).isoformat(),
                "issues": [],
            }
            store.put("install.operations", operation_id, record)
            self._emit(progress, operation_id, "preparing", 0, len(plan.changes))
            try:
                with ExitStack() as stack:
                    archives: dict[Path, zipfile.ZipFile] = {}
                    verified: dict[Path, str] = {}
                    for index, change in enumerate(plan.changes):
                        _check_cancelled(cancelled)
                        if change.source not in verified:
                            verify_file(
                                change.source,
                                (FileHash(HashAlgorithm.SHA256, change.source_sha256),),
                                cancelled=cancelled,
                            )
                            verified[change.source] = change.source_sha256
                        stage = self._slot(operation, "staging", index)
                        stream: Readable
                        if change.generated is not None:
                            import io

                            stream = stack.enter_context(io.BytesIO(change.generated))
                        elif change.member is None:
                            stream = stack.enter_context(change.source.open("rb"))
                        else:
                            if change.source not in archives:
                                archives[change.source] = stack.enter_context(
                                    zipfile.ZipFile(change.source)
                                )
                            stream = stack.enter_context(
                                archives[change.source].open(change.member)
                            )
                        try:
                            _copy(stream, stage, change.size, change.sha256, cancelled)
                        finally:
                            stream.close()
                        stage_proof = asdict(snapshot(stage))
                        before = asdict(change.before) if change.before is not None else None
                        backup_proof = None
                        if change.before is not None and change.action == "replace":
                            original = target_path(self.root, change.destination)
                            if snapshot(original, cancelled) != change.before:
                                raise ModMeshError(
                                    ErrorCode.CONFLICT, "Target changed before backup"
                                )
                            backup = self._slot(operation, "backups", index)
                            with original.open("rb") as original_stream:
                                _copy(
                                    original_stream,
                                    backup,
                                    change.before.size,
                                    change.before.sha256,
                                    cancelled,
                                )
                            backup_proof = asdict(snapshot(backup))
                        prepared_steps = array_record(record["steps"])
                        prepared_steps.append(
                            {
                                "path": change.destination,
                                "action": change.action,
                                "state": "prepared",
                                "stage": stage_proof,
                                "before": before,
                                "backup": backup_proof,
                                "owned_before": change.owned_before,
                            }
                        )
                        record["steps"] = prepared_steps
                        store.put("install.operations", operation_id, record)
                        self._emit(
                            progress,
                            operation_id,
                            "preparing",
                            index + 1,
                            len(plan.changes),
                            change.destination,
                        )
                    for path, sha in verified.items():
                        verify_file(
                            path, (FileHash(HashAlgorithm.SHA256, sha),), cancelled=cancelled
                        )
                record["state"] = "applying"
                store.put("install.operations", operation_id, record)
                steps = [object_record(value) for value in array_record(record["steps"])]
                new_files = {}
                for index, (change, step) in enumerate(zip(plan.changes, steps, strict=True)):
                    _check_cancelled(cancelled)
                    self._parents(change.destination, record, store)
                    target = target_path(self.root, change.destination)
                    if change.before is None:
                        if target.exists():
                            raise ModMeshError(
                                ErrorCode.CONFLICT, "A target appeared after plan approval"
                            )
                    elif snapshot(target, cancelled) != change.before:
                        raise ModMeshError(ErrorCode.CONFLICT, "Target changed after plan approval")
                    step["state"] = "publishing"
                    record["steps"] = steps
                    store.put("install.operations", operation_id, record)
                    if change.action == "replace":
                        moved = self._slot(operation, "moved", index)
                        os.rename(target, moved)
                        _sync_dir(target.parent)
                        _sync_dir(moved.parent)
                        if snapshot(moved) != change.before:
                            raise ModMeshError(
                                ErrorCode.CONFLICT, "Target was replaced during file transfer"
                            )
                    if change.action != "reuse":
                        publish_file(self._slot(operation, "staging", index), target)
                        _sync_dir(target.parent)
                    actual = snapshot(target, cancelled)
                    if actual.sha256 != change.sha256 or actual.size != change.size:
                        raise ModMeshError(
                            ErrorCode.INTEGRITY, "Applied file no longer matches the plan"
                        )
                    previous = old_files.get(change.destination.casefold())
                    previous_record = object_record(previous) if previous is not None else {}
                    owners = [
                        string_record(owner)
                        for owner in array_record(previous_record.get("owners", []))
                    ]
                    key = mod_key(change.project)
                    if key not in owners:
                        owners.append(key)
                    original_backup = previous_record.get("backup")
                    original_sha = previous_record.get("original_sha256")
                    if change.action == "replace" and not change.owned_before:
                        original_backup = f"{operation_id}/backups/{index}"
                        assert change.before is not None
                        original_sha = change.before.sha256
                    new_files[change.destination.casefold()] = {
                        "path": change.destination,
                        "sha256": actual.sha256,
                        "size": actual.size,
                        "device": actual.device,
                        "inode": actual.inode,
                        "managed": change.action != "reuse" or change.owned_before,
                        "owners": owners,
                        "backup": original_backup,
                        "original_sha256": original_sha,
                    }
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
                with store.transaction(write=True) as transaction:
                    for key, file_record in new_files.items():
                        transaction.put("install.files", key, file_record)
                    for mod in plan.resolution.mods:
                        recipe = next(item for item in plan.inputs if item.mod == mod)
                        reference = mod.project.reference
                        dependencies = tuple(
                            dict.fromkeys(
                                edge.target
                                for edge in plan.resolution.edges
                                if edge.parent == reference
                                and edge.declaration.kind == DependencyKind.REQUIRED
                            )
                        )
                        paths = tuple(
                            change.destination.casefold()
                            for change in plan.changes
                            if change.project == reference
                        )
                        transaction.put(
                            "install.mods",
                            mod_key(reference),
                            {
                                "mod": encode_mod(mod),
                                "paths": paths,
                                "dependencies": [asdict(ref) for ref in dependencies],
                                "operation_id": operation_id,
                                "rule_id": recipe.rule_id,
                                "prefix": recipe.prefix,
                            },
                        )
                    record["state"] = "committed"
                    transaction.put("install.operations", operation_id, record)
                self._cleanup(store, record)
                self._emit(progress, operation_id, "committed", len(steps), len(steps))
                return OperationResult(operation_id, "committed")
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, ModMeshError)
                    else ModMeshError(ErrorCode.STORAGE, "Installation failed before commit")
                )
                result = self._rollback(store, record)
                self._emit(progress, operation_id, result.state, 0, len(plan.changes))
                raise ModMeshError(
                    error.code,
                    error.message,
                    details={
                        "operation_id": operation_id,
                        "recovery": result.state,
                        "issues": result.issues,
                    },
                ) from exc

    def _rollback(self, store: MetadataStore, record: dict[str, object]) -> OperationResult:
        operation_id = string_record(record.get("id"))
        operation = self._operation_path(operation_id)
        issues = []
        identity = operation.stat(follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (record.get("device"), record.get("inode")):
            raise ModMeshError(
                ErrorCode.CONFLICT, "Operation directory was replaced; manual recovery required"
            )
        steps = [object_record(value) for value in array_record(record.get("steps", []))]
        for index in reversed(range(len(steps))):
            step = steps[index]
            if step.get("action") == "reuse" or step.get("state") == "prepared":
                continue
            try:
                target = target_path(self.root, string_record(step["path"]))
                stage_value = step.get("stage")
                stage_proof = object_record(stage_value) if stage_value is not None else None
                if stage_proof is not None and target.exists() and _same(target, stage_proof):
                    withdrawn = self._slot(operation, "withdrawn", index)
                    if withdrawn.exists():
                        raise ModMeshError(ErrorCode.CONFLICT, "Recovery file slot is occupied")
                    os.rename(target, withdrawn)
                    if not _same(withdrawn, stage_proof):
                        if not target.exists():
                            publish_file(withdrawn, target)
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Changed user file was preserved during recovery"
                        )
                    withdrawn.unlink()
                before_value = step.get("before")
                if before_value is None:
                    if target.exists():
                        raise ModMeshError(
                            ErrorCode.CONFLICT,
                            "Changed new target was preserved; manual recovery required",
                        )
                    continue
                before = object_record(before_value)
                moved = self._slot(operation, "moved", index)
                if target.exists():
                    if not _same(target, before):
                        raise ModMeshError(
                            ErrorCode.CONFLICT,
                            "Changed target was preserved; original backup remains",
                        )
                    continue
                if moved.exists():
                    if not _same(moved, before):
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Moved target changed; manual recovery required"
                        )
                    publish_file(moved, target)
                else:
                    backup_value = step.get("backup")
                    if backup_value is None:
                        raise ModMeshError(
                            ErrorCode.STORAGE, "Original file is missing from recovery slots"
                        )
                    backup = self._slot(operation, "backups", index)
                    if not _same(backup, object_record(backup_value)):
                        raise ModMeshError(
                            ErrorCode.INTEGRITY, "Recovery backup is missing or changed"
                        )
                    temporary = self._slot(operation, "withdrawn", index)
                    with backup.open("rb") as stream:
                        _copy(
                            stream,
                            temporary,
                            integer_record(before["size"]),
                            string_record(before["sha256"]),
                        )
                    publish_file(temporary, target)
                _sync_dir(target.parent)
            except (OSError, ModMeshError) as exc:
                reason = (
                    exc.message if isinstance(exc, ModMeshError) else "filesystem recovery failed"
                )
                issues.append(f"{step.get('path')}: {reason}")
        state = "recovery_required" if issues else "rolled_back"
        record["state"] = state
        record["issues"] = issues
        if not issues:
            with store.transaction(write=True) as transaction:
                for namespace, name in (
                    ("install.files", "old_files"),
                    ("install.mods", "old_mods"),
                ):
                    for key, value in object_record(record.get(name, {})).items():
                        if value is None:
                            transaction.delete(namespace, key)
                        else:
                            previous = object_record(value)
                            if namespace == "install.files" and previous.get("managed") is True:
                                path = target_path(self.root, string_record(previous["path"]))
                                if path.exists():
                                    current = snapshot(path)
                                    if current.sha256 == previous.get("sha256"):
                                        previous.update(device=current.device, inode=current.inode)
                            transaction.put(namespace, key, previous)
                transaction.put("install.operations", operation_id, record)
            self._cleanup(store, record)
            for value in reversed(array_record(record.get("created_dirs", []))):
                directory = object_record(value)
                with suppress(OSError, ModMeshError):
                    path = target_path(self.root, string_record(directory["path"]))
                    info = path.stat(follow_symlinks=False)
                    if path.is_dir() and (info.st_dev, info.st_ino) == (
                        directory.get("device"),
                        directory.get("inode"),
                    ):
                        path.rmdir()
        else:
            store.put("install.operations", operation_id, record)
        return OperationResult(operation_id, state, tuple(issues))

    def _cleanup(self, store: MetadataStore, record: dict[str, object]) -> None:
        if record.get("state") not in {"committed", "rolled_back"}:
            return
        operation_id = string_record(record["id"])
        operation = self._operation_path(operation_id)
        keep = {
            value.get("backup")
            for _, value in store.list("install.files")
            if isinstance(value.get("backup"), str)
        }
        for index, value in enumerate(array_record(record.get("steps", []))):
            step = object_record(value)
            for directory, name in (
                ("staging", "stage"),
                ("backups", "backup"),
                ("moved", "before"),
            ):
                proof = step.get(name)
                if proof is None or f"{operation_id}/{directory}/{index}" in keep:
                    continue
                with suppress(OSError, ModMeshError):
                    path = self._slot(operation, directory, index)
                    if path.exists() and _same(path, object_record(proof)):
                        path.unlink()
        for name in ("staging", "backups", "moved", "withdrawn"):
            with suppress(OSError):
                (operation / name).rmdir()

    def recover(self) -> tuple[OperationResult, ...]:
        results = []
        with self._locked() as store:
            if store is None:
                return ()
            root = self.root.stat(follow_symlinks=False)
            for key, record in store.list("install.operations"):
                if record.get("state") in _TERMINAL:
                    continue
                if record.get("game_id") != self.installation.game_id or (
                    root.st_dev,
                    root.st_ino,
                ) != (record.get("root_device"), record.get("root_inode")):
                    results.append(
                        OperationResult(
                            key,
                            "recovery_required",
                            ("Game root identity differs from the operation journal",),
                        )
                    )
                    continue
                try:
                    idle = self.games.get(self.installation.game_id).validate_operation(
                        self.installation
                    )
                    if not idle.valid:
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "; ".join(issue.message for issue in idle.issues)
                        )
                    results.append(self._rollback(store, record))
                except (OSError, ModMeshError) as exc:
                    results.append(
                        OperationResult(
                            key,
                            "recovery_required",
                            (
                                exc.message
                                if isinstance(exc, ModMeshError)
                                else "Operation recovery failed",
                            ),
                        )
                    )
        return tuple(results)
