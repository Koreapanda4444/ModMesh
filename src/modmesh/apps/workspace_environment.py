from __future__ import annotations

from dataclasses import dataclass

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_legacy import activation_target
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_identity import GameIdentity, capture_game_identity
from modmesh.core.lockfiles import PackLock
from modmesh.core.packs import PackContext
from modmesh.core.workspace_history import WorkspaceHistory
from modmesh.core.workspaces import Workspace, document_digest
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore
from modmesh.storage.rules import load_rules


@dataclass(frozen=True)
class EnvironmentDrift:
    before: dict[str, object] | None
    after: dict[str, object]
    changes: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return document_digest(self.document())

    def document(self) -> dict[str, object]:
        return {
            "state": "drift" if self.changes else "unchanged",
            "before": self.before,
            "after": self.after,
            "changes": list(self.changes),
            "game_downgrade_supported": False,
        }


def observe_environment(
    settings: ClientSettings, installation: GameInstallation
) -> dict[str, object]:
    rules = load_rules(settings.paths.database)
    registered = rules.get(installation.game_id)
    adapter = create_game_registry(settings).get(installation.game_id)
    current = adapter.inspect(installation.path, installation.platform)
    if current is None:
        raise ModMeshError(ErrorCode.CONFLICT, "Game path is no longer recognized")
    identity = capture_game_identity(current, registered.rule)
    root = current.path.stat(follow_symlinks=False)
    return {
        "target": activation_target(current),
        "context": identity.context.document(),
        "identity": identity.document(),
        "rule_sha256": registered.rule.digest,
        "root_device": root.st_dev,
        "root_inode": root.st_ino,
    }


def inspect_workspace_environment(
    settings: ClientSettings, workspace: Workspace, installation: GameInstallation
) -> EnvironmentDrift:
    if workspace.game_id != installation.game_id:
        raise ModMeshError(ErrorCode.CONFLICT, "Workspace environment belongs to another game")
    after = observe_environment(settings, installation)
    key = f"{workspace.pack_id}:{activation_target(installation)}"
    with MetadataStore(settings.paths.database) as database:
        before = database.get("workspaces.environments", key)
        if before is None:
            history = WorkspaceHistory(database).list(workspace.pack_id)
            if history:
                lock = history[-1].lock
                before = {"context": lock.context.document(), "rule_sha256": lock.rule_sha256}
                if lock.game_identity is not None:
                    before["identity"] = lock.game_identity.document()
                receipts = [
                    record
                    for _, record in database.list("workspaces.receipts")
                    if record.get("pack_id") == workspace.pack_id
                    and record.get("target") == after["target"]
                ]
                if receipts:
                    latest = max(receipts, key=lambda item: str(item.get("created_at", "")))
                    for field in ("target", "root_device", "root_inode"):
                        before[field] = latest[field]
    changes = tuple(key for key, value in (before or {}).items() if value != after.get(key))
    return EnvironmentDrift(before, after, changes)


def require_workspace_environment(
    settings: ClientSettings, workspace: Workspace, installation: GameInstallation
) -> None:
    drift = inspect_workspace_environment(settings, workspace, installation)
    if drift.changes:
        raise ModMeshError(
            ErrorCode.CONFLICT,
            "Game, loader, rule or path changed; review an update or experimental branch",
            details=drift.document(),
        )
    key = f"{workspace.pack_id}:{activation_target(installation)}"
    with (
        MetadataStore(settings.paths.database) as database,
        database.transaction(write=True) as transaction,
    ):
        existing = transaction.get("workspaces.environments", key)
        if existing is not None and existing != drift.after:
            raise ModMeshError(ErrorCode.CONFLICT, "Environment baseline changed concurrently")
        transaction.put("workspaces.environments", key, drift.after)


def verify_locked_environment(
    settings: ClientSettings, installation: GameInstallation, lock: PackLock
) -> None:
    current = observe_environment(settings, installation)
    if (
        PackContext.read(current["context"]) != lock.context
        or current["rule_sha256"] != lock.rule_sha256
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Locked game/loader/rule no longer matches")
    if (
        lock.game_identity is not None
        and GameIdentity.read(current["identity"]) != lock.game_identity
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Game or loader file identity changed")
