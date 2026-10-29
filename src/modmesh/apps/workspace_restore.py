from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_activation import apply_workspace
from modmesh.apps.workspace_legacy import activation_target, profile_configuration_digest
from modmesh.apps.workspace_updates import WorkspaceUpdate, prepare_update, save_update
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.installation import OperationProgress, _check_cancelled
from modmesh.core.lockfiles import PackLock
from modmesh.core.profiles import ProfileManager
from modmesh.core.workspace_history import WorkspaceHistory
from modmesh.core.workspace_receipts import ActivationReceipt
from modmesh.core.workspaces import WorkspaceStore, document_digest
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class WorkspaceRestore:
    source: PackLock
    update: WorkspaceUpdate

    @property
    def fingerprint(self) -> str:
        return document_digest({"restore": self.source.digest, "update": self.update.fingerprint})

    def document(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "applicable": self.update.applicable,
            "source_snapshot": self.source.digest,
            "source_revision": self.source.workspace.revision,
            "historical_game": self.source.context.document(),
            "current_game": self.update.activation.lock.context.document()
            if self.update.activation
            else None,
            "warning": "Only the mod configuration is restored. The game itself is not downgraded; "
            "runtime loading remains unverified.",
            "game_downgrade_supported": False,
            "update": self.update.document(),
        }


async def prepare_restore(
    settings: ClientSettings,
    pack_id: str,
    snapshot: str,
    installation: GameInstallation,
    expected: str,
    *,
    cancelled: threading.Event | None = None,
) -> WorkspaceRestore:
    with MetadataStore(settings.paths.database) as database:
        current = WorkspaceStore(database).get(pack_id)
        source = WorkspaceHistory(database).get(snapshot).lock
        receipts = [
            record
            for _, record in database.list("workspaces.receipts")
            if record.get("lock_digest") == snapshot
            and record.get("target") == activation_target(installation)
        ]
    if (
        source.workspace.pack_id != pack_id
        or source.workspace.game_id != current.game_id
        or current.digest != expected
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Restore snapshot or workspace identity differs")
    retained = None
    if receipts:
        receipt = max(receipts, key=lambda item: str(item.get("created_at", "")))
        manager = ProfileManager(create_game_registry(settings), installation)
        retained = next(
            (item for item in manager.profiles() if item.profile_id == receipt.get("profile_id")),
            None,
        )
        if retained is not None and profile_configuration_digest(retained) != receipt.get(
            "profile_digest"
        ):
            raise ModMeshError(
                ErrorCode.INTEGRITY, "Historical profile receipt differs from retained content"
            )
    overrides = tuple(item.mod.project.reference for item in current.entries)
    update = await prepare_update(
        settings,
        pack_id,
        installation,
        expected,
        entries=source.workspace.entries,
        settings_files=source.workspace.settings,
        allow_pinned=overrides,
        cancelled=cancelled,
        template=source.workspace,
        retained_profile=retained,
    )
    return WorkspaceRestore(source, update)


def apply_restore(
    settings: ClientSettings,
    plan: WorkspaceRestore,
    approval: str,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[OperationProgress], None] | None = None,
) -> ActivationReceipt:
    cancelled = cancelled or threading.Event()
    if approval != plan.fingerprint or not plan.update.applicable or plan.update.activation is None:
        raise ModMeshError(
            ErrorCode.CONFLICT, "Approve the exact applicable mod configuration restore"
        )
    _check_cancelled(cancelled.is_set)
    saved = save_update(settings, plan.update, plan.update.fingerprint)
    try:
        return apply_workspace(
            settings,
            plan.update.activation,
            plan.update.activation.fingerprint,
            cancelled=cancelled,
            progress=progress,
        )
    except ModMeshError as exc:
        raise ModMeshError(
            exc.code,
            exc.message,
            details={
                **exc.details,
                "desired_workspace_digest": saved.digest,
                "desired_revision_saved": saved.revision,
                "active_revision_unchanged_unless_receipt_committed": True,
            },
        ) from exc
