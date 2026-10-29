from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_activation import WorkspaceActivation, prepare_locked_workspace
from modmesh.apps.workspace_environment import (
    EnvironmentDrift,
    inspect_workspace_environment,
    verify_locked_environment,
)
from modmesh.apps.workspace_legacy import activation_target
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.installation import _check_cancelled
from modmesh.core.lockfiles import build_lock
from modmesh.core.packs import PackEntry, PackSetting
from modmesh.core.workspace_history import PackSnapshot
from modmesh.core.workspace_receipts import WorkspaceReceipts
from modmesh.core.workspaces import (
    Workspace,
    WorkspaceStore,
    document_digest,
    require_workspace_editable,
)
from modmesh.domain.models import GameInstallation, SourceRef
from modmesh.storage.metadata import MetadataStore
from modmesh.storage.rules import load_rules


@dataclass(frozen=True)
class WorkspaceUpdate:
    before: Workspace
    proposed: Workspace
    environment: EnvironmentDrift
    baseline: dict[str, object] | None
    activation: WorkspaceActivation | None
    issues: tuple[str, ...]
    overrides: tuple[SourceRef, ...] = ()

    @property
    def applicable(self) -> bool:
        return not self.issues and self.activation is not None and self.activation.applicable

    @property
    def fingerprint(self) -> str:
        return document_digest(
            {
                "before": self.before.digest,
                "proposed": self.proposed.digest,
                "environment": self.environment.document(),
                "baseline": self.baseline,
                "activation": self.activation.fingerprint if self.activation else None,
                "issues": self.issues,
                "overrides": [(ref.source_id, ref.project_id) for ref in self.overrides],
            }
        )

    def document(self) -> dict[str, object]:
        old = {entry.mod.project.reference: entry.document() for entry in self.before.entries}
        new = {entry.mod.project.reference: entry.document() for entry in self.proposed.entries}

        def label(ref: SourceRef) -> str:
            return ref.source_id + ":" + ref.project_id

        return {
            "fingerprint": self.fingerprint,
            "applicable": self.applicable,
            "pack_id": self.before.pack_id,
            "from_revision": self.before.revision,
            "to_revision": self.proposed.revision,
            "environment": self.environment.document(),
            "added": [label(ref) for ref in sorted(new.keys() - old.keys())],
            "removed": [label(ref) for ref in sorted(old.keys() - new.keys())],
            "updated": [
                label(ref) for ref in sorted(old.keys() & new.keys()) if old[ref] != new[ref]
            ],
            "issues": list(self.issues),
            "activation": self.activation.document() if self.activation else None,
            "game_files_changed_on_save": False,
            "game_downgrade_supported": False,
        }


async def prepare_update(
    settings: ClientSettings,
    pack_id: str,
    installation: GameInstallation,
    expected: str,
    *,
    entries: tuple[PackEntry, ...] | None = None,
    settings_files: tuple[PackSetting, ...] | None = None,
    allow_pinned: tuple[SourceRef, ...] = (),
    cancelled: threading.Event | None = None,
) -> WorkspaceUpdate:
    cancelled = cancelled or threading.Event()
    _check_cancelled(cancelled.is_set)
    with MetadataStore(settings.paths.database) as database:
        before = WorkspaceStore(database).get(pack_id)
        baseline = database.get(
            "workspaces.environments", f"{pack_id}:{activation_target(installation)}"
        )
        if WorkspaceReceipts(database).pending(activation_target(installation)):
            raise ModMeshError(ErrorCode.CONFLICT, "Recover pending activation before updating")
    if before.digest != expected or before.game_id != installation.game_id:
        raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed or belongs to a different game")
    entries = entries if entries is not None else before.entries
    refs = {entry.mod.project.reference for entry in entries}
    proposed = Workspace.read(
        replace(
            before,
            revision=before.revision + 1,
            entries=entries,
            disabled=tuple(ref for ref in before.disabled if ref in refs),
            unpinned=tuple(ref for ref in before.unpinned if ref in refs),
            order=tuple(ref for ref in before.order if ref in refs),
            settings=before.settings if settings_files is None else settings_files,
        ).document()
    )
    environment = inspect_workspace_environment(settings, before, installation)
    issues = []
    replacements = {entry.mod.project.reference: entry for entry in proposed.entries}
    if len(set(allow_pinned)) != len(allow_pinned) or any(
        ref not in {item.mod.project.reference for item in before.entries} for ref in allow_pinned
    ):
        raise ModMeshError(
            ErrorCode.VALIDATION, "Pin overrides must name distinct existing entries"
        )
    for entry in before.entries:
        ref = entry.mod.project.reference
        selected = replacements.get(ref)
        if (
            ref not in before.unpinned
            and ref not in allow_pinned
            and (selected is None or selected.mod != entry.mod)
        ):
            issues.append(
                f"Pinned version requires an explicit override: {ref.source_id}:{ref.project_id}"
            )
    activation = None
    if not issues:
        adapter = create_game_registry(settings).get(before.game_id)
        current = adapter.inspect(installation.path, installation.platform)
        if current is None:
            raise ModMeshError(ErrorCode.CONFLICT, "Game disappeared during update planning")
        try:
            lock = build_lock(
                proposed, current, adapter, load_rules(settings.paths.database).get(before.game_id)
            )
            activation = await prepare_locked_workspace(
                settings, lock, current, cancelled=cancelled
            )
            if not activation.applicable and activation.plan is not None:
                issues.extend(item.message for item in activation.plan.issues)
        except ModMeshError as exc:
            if exc.code == ErrorCode.CANCELLED:
                raise
            issues.append(exc.message)
    return WorkspaceUpdate(
        before,
        proposed,
        environment,
        baseline,
        activation,
        tuple(issues),
        tuple(sorted(allow_pinned)),
    )


def save_update(settings: ClientSettings, plan: WorkspaceUpdate, approval: str) -> Workspace:
    if not plan.applicable or approval != plan.fingerprint or plan.activation is None:
        raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact applicable update plan")
    prepared = plan.activation
    installation = prepared.manager.installation
    verify_locked_environment(settings, installation, prepared.lock)
    current_environment = inspect_workspace_environment(settings, plan.before, installation)
    if current_environment != plan.environment:
        raise ModMeshError(ErrorCode.CONFLICT, "Environment changed after update approval")
    if prepared.plan is not None:
        checked = prepared.manager.plan_switch(prepared.profile_id)
        if not checked.applicable or checked.fingerprint != prepared.plan.fingerprint:
            raise ModMeshError(ErrorCode.CONFLICT, "Files changed after update preview")
    else:
        with prepared.manager.verified_active(prepared.profile_id):
            pass
    key = f"{plan.before.pack_id}:{prepared.target}"
    snapshot = PackSnapshot(prepared.lock, datetime.now(UTC).isoformat())
    revision_key = f"{plan.proposed.pack_id}:{plan.proposed.revision}"
    with (
        MetadataStore(settings.paths.database) as database,
        database.transaction(write=True) as transaction,
    ):
        require_workspace_editable(transaction, plan.before.pack_id)
        current = Workspace.read(transaction.get("workspaces.items", plan.before.pack_id))
        if current.digest != plan.before.digest or plan.proposed.revision != current.revision + 1:
            raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed after update preview")
        if transaction.get("workspaces.environments", key) != plan.baseline:
            raise ModMeshError(ErrorCode.CONFLICT, "Environment baseline changed after preview")
        if transaction.get("workspaces.revisions", revision_key) is not None:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Update revision already has an immutable snapshot"
            )
        if transaction.get("workspaces.active", prepared.target) != prepared.previous_receipt:
            raise ModMeshError(ErrorCode.CONFLICT, "Active pack changed after update preview")
        transaction.put("workspaces.items", plan.proposed.pack_id, plan.proposed.document())
        transaction.put("workspaces.environments", key, plan.environment.after)
        transaction.put("workspaces.snapshots", prepared.lock.digest, snapshot.document())
        transaction.put("workspaces.revisions", revision_key, {"digest": prepared.lock.digest})
        constraint = transaction.get("workspaces.constraints", plan.proposed.pack_id)
        if constraint is not None:
            transaction.put(
                "workspaces.constraints",
                plan.proposed.pack_id,
                {"context": prepared.lock.context.document()},
            )
    return plan.proposed
