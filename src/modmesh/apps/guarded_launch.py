import threading
from datetime import UTC, datetime
from uuid import uuid4

from modmesh.adapters.launch import NativeLaunchBoundary, verify_launch_files
from modmesh.apps.launch_planning import prepare_launch
from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_activation import _activation_lease
from modmesh.apps.workspace_environment import verify_locked_environment
from modmesh.apps.workspace_legacy import activation_target, profile_configuration_digest
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.installation import _check_cancelled
from modmesh.core.launching import (
    LaunchBoundary,
    LaunchCall,
    LaunchCallState,
    LaunchMethod,
    LaunchPlan,
)
from modmesh.core.profiles import ProfileManager
from modmesh.core.workspace_history import WorkspaceHistory
from modmesh.core.workspace_receipts import ActivationReceipt
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore


def guarded_launch(
    settings: ClientSettings,
    installation: GameInstallation,
    plan: LaunchPlan,
    receipt: ActivationReceipt,
    approval: str,
    *,
    boundary: LaunchBoundary | None = None,
    cancelled: threading.Event | None = None,
) -> dict[str, object]:
    cancellation = cancelled or threading.Event()
    if (
        approval != plan.fingerprint
        or receipt.target != plan.target
        or plan.target != activation_target(installation)
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact launch target and plan")
    with _activation_lease(settings, plan.target):
        with MetadataStore(settings.paths.database) as database:
            if database.get("workspaces.active", plan.target) != receipt.document():
                raise ModMeshError(ErrorCode.CONFLICT, "Applied pack changed before launch")
            lock = WorkspaceHistory(database).get(receipt.lock_digest).lock
        verify_locked_environment(settings, installation, lock)
        manager = ProfileManager(create_game_registry(settings), installation)
        with manager.verified_active_state(receipt.profile_id) as (profile, store):
            if profile_configuration_digest(profile) != receipt.profile_digest:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Applied profile content changed before launch"
                )
            current = prepare_launch(settings, installation)
            if current.fingerprint != approval:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Launch configuration changed; review it again"
                )
            verify_launch_files(plan)
            _check_cancelled(cancellation.is_set)
            record: dict[str, object] = {
                "schema_version": 1,
                "attempt_id": str(uuid4()),
                "target": plan.target,
                "activation": receipt.document(),
                "launch_fingerprint": plan.fingerprint,
                "method": plan.method.value,
                "state": "invoking",
                "created_at": datetime.now(UTC).isoformat(),
                "game_process_observed": False,
                "mod_loading": "unverified",
            }
            store.put("launch.guard", "active", record)
            if plan.method == LaunchMethod.MANUAL:
                call = LaunchCall(LaunchCallState.MANUAL, detail=plan.instruction)
            else:
                call = (boundary or NativeLaunchBoundary()).invoke(plan)
            record.update(call.document())
            store.put("launch.guard", "active", record)
            return record
