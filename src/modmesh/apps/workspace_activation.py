from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_artifacts import artifact_vault
from modmesh.apps.workspace_environment import verify_locked_environment
from modmesh.apps.workspace_legacy import activation_target, profile_configuration_digest
from modmesh.apps.workspace_lifecycle import freeze_workspace
from modmesh.core.cache import ArtifactCache, _Lease
from modmesh.core.compatibility import inspect_compatibility
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import _blocking
from modmesh.core.games import GameCapability
from modmesh.core.installation import OperationProgress, OperationResult, _check_cancelled
from modmesh.core.lockfiles import PackLock
from modmesh.core.planning import InstallInput
from modmesh.core.profiles import ProfileManager, ProfilePlan
from modmesh.core.provenance import record_verified_artifact
from modmesh.core.workspace_history import WorkspaceHistory
from modmesh.core.workspace_receipts import ActivationReceipt, WorkspaceReceipts
from modmesh.core.workspaces import document_digest
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class WorkspaceActivation:
    lock: PackLock
    manager: ProfileManager
    plan: ProfilePlan | None
    profile_id: str
    profile_digest: str
    previous_receipt: dict[str, object] | None

    @property
    def target(self) -> str:
        return activation_target(self.manager.installation)

    @property
    def applicable(self) -> bool:
        return self.plan is None or self.plan.applicable

    @property
    def fingerprint(self) -> str:
        return document_digest(
            {
                "lock": self.lock.digest,
                "target": self.target,
                "profile_plan": self.plan.fingerprint if self.plan else None,
                "profile_id": self.profile_id,
                "profile_digest": self.profile_digest,
                "previous_receipt": self.previous_receipt,
            }
        )

    def document(self) -> dict[str, object]:
        return {
            "pack_id": self.lock.workspace.pack_id,
            "revision": self.lock.workspace.revision,
            "lock_digest": self.lock.digest,
            "fingerprint": self.fingerprint,
            "game_path": str(self.manager.root),
            "applicable": self.applicable,
            "already_active": self.plan is None,
            "compatibility": inspect_compatibility(
                self.lock, self.plan.issues if self.plan else ()
            ).document(),
            "changes": [
                {"action": item.action, "path": item.path, "size": item.size}
                for item in self.plan.changes
            ]
            if self.plan
            else [],
            "issues": [{"code": item.code, "message": item.message} for item in self.plan.issues]
            if self.plan
            else [],
            "warnings": [*self.lock.warnings, *(self.plan.warnings if self.plan else ())],
        }


async def prepare_workspace(
    settings: ClientSettings,
    pack_id: str,
    installation: GameInstallation,
    expected: str,
    *,
    cancelled: threading.Event | None = None,
) -> WorkspaceActivation:
    cancelled = cancelled or threading.Event()
    lock = await _blocking(
        partial(freeze_workspace, settings, pack_id, installation, expected), cancelled
    )
    games = create_game_registry(settings)
    if GameCapability.PROFILES not in games.get(installation.game_id).capabilities:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Game does not support managed pack activation")
    manager = ProfileManager(games, installation)
    target = activation_target(installation)
    key = f"{target}:{pack_id}"
    with MetadataStore(settings.paths.database) as database:
        if WorkspaceReceipts(database).pending(target):
            raise ModMeshError(ErrorCode.CONFLICT, "Recover the pending pack activation first")
        binding = database.get("workspaces.profiles", key)
        previous = database.get("workspaces.active", target)
    matches = binding is not None and (
        binding.get("selection_digest") == lock.workspace.selection_digest
        or binding.get("workspace_digest") == lock.workspace.digest
    )
    if matches and binding is not None:
        profile = next(
            (item for item in manager.profiles() if item.profile_id == binding.get("profile_id")),
            None,
        )
        if (
            profile is not None
            and profile.context == manager.context()
            and profile_configuration_digest(profile) == binding.get("profile_digest")
        ):
            if manager.active_id() == profile.profile_id:
                captured = await _blocking(manager.capture_active, cancelled)
                if profile_configuration_digest(captured) != binding.get("profile_digest"):
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "Active workspace content changed; review a new plan"
                    )
                return WorkspaceActivation(
                    lock,
                    manager,
                    None,
                    profile.profile_id,
                    profile_configuration_digest(captured),
                    previous,
                )
            plan = await _blocking(partial(manager.plan_switch, profile.profile_id), cancelled)
            return WorkspaceActivation(
                lock,
                manager,
                plan,
                profile.profile_id,
                profile_configuration_digest(profile),
                previous,
            )
    if lock.workspace.settings:
        raise ModMeshError(
            ErrorCode.CONFLICT, "Settings content is unavailable; use its retained legacy profile"
        )
    inputs = []
    with (
        artifact_vault(settings) as vault,
        ArtifactCache(settings.paths.cache_dir, max_bytes=settings.cache_max_bytes) as cache,
    ):
        for entry in lock.entries:
            if cancelled.is_set():
                raise ModMeshError(ErrorCode.CANCELLED, "Workspace preparation cancelled")
            artifact = await _blocking(
                partial(vault.lookup, entry.mod.file, cancelled=cancelled), cancelled
            )
            if artifact is None:
                artifact = await _blocking(
                    partial(cache.lookup, entry.mod.file, cancelled=cancelled), cancelled
                )
            if artifact is None:
                raise ModMeshError(
                    ErrorCode.NOT_FOUND,
                    "Artifact unavailable; acquire the exact permitted source file first",
                )
            inputs.append(InstallInput(entry.mod, artifact.path, entry.rule_id, entry.prefix))
            with MetadataStore(settings.paths.database) as database:
                record_verified_artifact(database, entry, artifact)
        payload = await _blocking(
            partial(
                manager.payload_plan, lock.resolution(), tuple(inputs), cancelled=cancelled.is_set
            ),
            cancelled,
        )
        if payload.issues:
            raise ModMeshError(
                ErrorCode.CONFLICT,
                "Workspace payload failed safety checks",
                details={"issues": [item.message for item in payload.issues]},
            )
        plan = await _blocking(
            partial(
                manager.stage_payload,
                payload,
                (lock.workspace.name + f" r{lock.workspace.revision}")[:100],
            ),
            cancelled,
        )
    profile_digest = profile_configuration_digest(plan.target)
    with MetadataStore(settings.paths.database) as database:
        database.put(
            "workspaces.profiles",
            key,
            {
                "pack_id": pack_id,
                "target": target,
                "workspace_digest": lock.workspace.digest,
                "selection_digest": lock.workspace.selection_digest,
                "lock_digest": lock.digest,
                "profile_id": plan.target.profile_id,
                "profile_digest": profile_digest,
                "origin": "workspace",
            },
        )
    return WorkspaceActivation(
        lock, manager, plan, plan.target.profile_id, profile_digest, previous
    )


@contextmanager
def _activation_lease(settings: ClientSettings, target: str) -> Iterator[None]:
    directory = settings.paths.data_dir / "activation-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.is_junction():
        raise ModMeshError(ErrorCode.STORAGE, "Indirect activation lock directory blocked")
    lease = _Lease(directory / target)
    try:
        if not lease.acquire():
            raise ModMeshError(ErrorCode.CONFLICT, "Another pack activation or recovery is active")
        yield
    finally:
        lease.close()


def _verify_root(manager: ProfileManager, receipt: ActivationReceipt) -> None:
    root = manager.root.stat(follow_symlinks=False)
    if (root.st_dev, root.st_ino) != (receipt.root_device, receipt.root_inode):
        raise ModMeshError(ErrorCode.CONFLICT, "Game root changed; manual recovery is required")


def _finish_activation(
    settings: ClientSettings, manager: ProfileManager, record: dict[str, object]
) -> ActivationReceipt:
    receipt = ActivationReceipt.read(record.get("receipt"))
    _verify_root(manager, receipt)
    with MetadataStore(settings.paths.database) as database:
        locked = WorkspaceHistory(database).get(receipt.lock_digest).lock
    verify_locked_environment(settings, manager.installation, locked)
    with manager.verified_active(receipt.profile_id) as profile:
        if profile_configuration_digest(profile) != receipt.profile_digest:
            raise ModMeshError(ErrorCode.CONFLICT, "Activated profile content differs from receipt")
        with MetadataStore(settings.paths.database) as database:
            return WorkspaceReceipts(database).complete(record)


def apply_workspace(
    settings: ClientSettings,
    prepared: WorkspaceActivation,
    approval: str,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[OperationProgress], None] | None = None,
) -> ActivationReceipt:
    cancelled = cancelled or threading.Event()
    if not prepared.applicable or approval != prepared.fingerprint:
        raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact applicable pack activation plan")
    with _activation_lease(settings, prepared.target):
        _check_cancelled(cancelled.is_set)
        workspace, manager = prepared.lock.workspace, prepared.manager
        current = freeze_workspace(
            settings, workspace.pack_id, manager.installation, workspace.digest
        )
        if current.digest != prepared.lock.digest:
            raise ModMeshError(ErrorCode.CONFLICT, "Pack lock or rule changed; review a new plan")
        previous_id = prepared.plan.active_id if prepared.plan else prepared.profile_id
        if previous_id is None or manager.active_id() != previous_id:
            raise ModMeshError(ErrorCode.CONFLICT, "Active profile changed; review a new plan")
        root = manager.root.stat(follow_symlinks=False)
        receipt = ActivationReceipt(
            prepared.plan.plan_id if prepared.plan else str(uuid4()),
            prepared.target,
            workspace.pack_id,
            prepared.lock.digest,
            prepared.profile_id,
            prepared.profile_digest,
            previous_id,
            root.st_dev,
            root.st_ino,
            datetime.now(UTC).isoformat(),
        )
        with MetadataStore(settings.paths.database) as database:
            WorkspaceHistory(database).capture(prepared.lock)
            record = WorkspaceReceipts(database).begin(
                receipt, prepared.previous_receipt, workspace.digest, noop=prepared.plan is None
            )
        try:
            _check_cancelled(cancelled.is_set)
            if prepared.plan is not None:
                manager.switch(
                    prepared.plan,
                    prepared.plan.fingerprint,
                    cancelled=cancelled.is_set,
                    progress=progress,
                )
            return _finish_activation(settings, manager, record)
        except Exception as exc:
            state = "recovery_required"
            with suppress(OSError, ModMeshError):
                core = next(
                    (
                        item
                        for item in manager.history()
                        if item.operation_id == receipt.operation_id
                    ),
                    None,
                )
                if core is None and prepared.plan is not None:
                    state = "aborted"
                elif core is not None and core.state in {"rolled_back", "failed"}:
                    state = "rolled_back"
                with MetadataStore(settings.paths.database) as database:
                    WorkspaceReceipts(database).mark(record, state, (str(exc),))
            raise ModMeshError(
                exc.code if isinstance(exc, ModMeshError) else ErrorCode.STORAGE,
                "Pack activation did not finish; "
                + (
                    "recover its receipt before continuing"
                    if state == "recovery_required"
                    else str(exc)
                ),
                details={"operation_id": receipt.operation_id, "recovery": state},
            ) from exc


def recover_workspace(
    settings: ClientSettings, installation: GameInstallation
) -> tuple[OperationResult, ...]:
    target = activation_target(installation)
    manager = ProfileManager(create_game_registry(settings), installation)
    with _activation_lease(settings, target):
        core_results = manager.recover()
        core_history = {item.operation_id: item for item in manager.history()}
        with MetadataStore(settings.paths.database) as database:
            pending = WorkspaceReceipts(database).pending(target)
        results = []
        issues: tuple[str, ...]
        for record in pending:
            receipt = ActivationReceipt.read(record.get("receipt"))
            try:
                _verify_root(manager, receipt)
                core = core_history.get(receipt.operation_id)
                if core is not None and core.state not in {"committed", "rolled_back", "failed"}:
                    raise ModMeshError(ErrorCode.CONFLICT, "File recovery still requires attention")
                active = manager.active_id()
                committed = core is not None and core.state == "committed"
                if (committed or record.get("noop") is True) and active == receipt.profile_id:
                    _finish_activation(settings, manager, record)
                    results.append(OperationResult(receipt.operation_id, "committed"))
                    continue
                if committed or active != receipt.previous_profile_id:
                    state, issues = (
                        "superseded",
                        ("A different profile is active; files were preserved",),
                    )
                else:
                    with manager.verified_active(receipt.previous_profile_id):
                        pass
                    state, issues = ("rolled_back" if core is not None else "aborted"), ()
                with MetadataStore(settings.paths.database) as database:
                    WorkspaceReceipts(database).mark(record, state, issues)
                results.append(OperationResult(receipt.operation_id, state, issues))
            except (OSError, ModMeshError) as exc:
                issues = (str(exc),)
                with MetadataStore(settings.paths.database) as database:
                    WorkspaceReceipts(database).mark(record, "recovery_required", issues)
                results.append(OperationResult(receipt.operation_id, "recovery_required", issues))
        workspace_ids = {item.operation_id for item in results}
        return tuple(results) + tuple(
            item for item in core_results if item.operation_id not in workspace_ids
        )


def active_workspace(settings: ClientSettings, installation: GameInstallation) -> dict[str, object]:
    target = activation_target(installation)
    with MetadataStore(settings.paths.database) as database:
        pending = WorkspaceReceipts(database).pending(target)
        value = database.get("workspaces.active", target)
        if pending:
            return {"state": "recovery_required", "pending": len(pending), "receipt": value}
        if value is None:
            return {"state": "none", "receipt": None}
        receipt = ActivationReceipt.read(value)
        saved = database.get("workspaces.receipts", receipt.operation_id)
        lock = WorkspaceHistory(database).get(receipt.lock_digest).lock
    if receipt.target != target or saved != value or lock.workspace.pack_id != receipt.pack_id:
        raise ModMeshError(ErrorCode.INTEGRITY, "Active receipt and immutable history differ")
    manager = ProfileManager(create_game_registry(settings), installation)
    try:
        _verify_root(manager, receipt)
        verify_locked_environment(settings, manager.installation, lock)
        with manager.verified_active(receipt.profile_id) as profile:
            if profile_configuration_digest(profile) != receipt.profile_digest:
                raise ModMeshError(ErrorCode.CONFLICT, "Active pack content was modified")
    except (OSError, ModMeshError) as exc:
        return {"state": "unverified", "receipt": value, "issue": str(exc)}
    return {"state": "active", "receipt": value, "warnings": list(lock.warnings)}
