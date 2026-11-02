from dataclasses import dataclass
from datetime import UTC, datetime

import psutil

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_activation import _activation_lease
from modmesh.apps.workspace_legacy import activation_target
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.launch_guard import launch_hold
from modmesh.core.launch_receipts import read_launch_receipt, save_launch_receipt
from modmesh.core.profiles import ProfileManager
from modmesh.core.workspace_receipts import ActivationReceipt
from modmesh.core.workspaces import document_digest
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class LaunchRelease:
    target: str
    guard: dict[str, object]

    @property
    def fingerprint(self) -> str:
        return document_digest({"target": self.target, "guard": self.guard})

    def document(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "attempt_id": self.guard["attempt_id"],
            "state": self.guard["state"],
            "target": self.target,
            "instruction": "Close the game and launcher, cancel every queued launch, "
            "then explicitly confirm release. This does not launch or change any pack files.",
            "pack_preserved": True,
            "automatic_retry": False,
        }


def _release_ready(manager: ProfileManager, guard: dict[str, object]) -> None:
    receipt = ActivationReceipt.read(guard["activation"])
    root = manager.root.stat(follow_symlinks=False)
    if (
        manager.root.is_symlink()
        or manager.root.is_junction()
        or (root.st_dev, root.st_ino) != (receipt.root_device, receipt.root_inode)
    ):
        raise ModMeshError(
            ErrorCode.CONFLICT, "Launch root changed; preserve its state for manual review"
        )
    report = manager.games.get(manager.installation.game_id).validate_operation(
        manager.installation
    )
    if not report.valid:
        raise ModMeshError(
            ErrorCode.CONFLICT,
            "Close the selected game before releasing the launch guard",
            details={"issues": [item.message for item in report.issues]},
        )
    pid, started = guard.get("pid"), guard.get("process_started_at")
    if isinstance(pid, int):
        try:
            process = psutil.Process(pid)
            same = started is None or process.create_time() == started
            if same and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Close the launched process before releasing its guard"
                )
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Launch process is unreadable; close it first"
            ) from exc


def prepare_launch_release(
    settings: ClientSettings, installation: GameInstallation
) -> LaunchRelease:
    target = activation_target(installation)
    manager = ProfileManager(create_game_registry(settings), installation)
    with manager._locked() as store:
        guard = launch_hold(store)
        if guard is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "There is no pending launch guard to release")
        guard = read_launch_receipt(guard)
        if guard["target"] != target:
            raise ModMeshError(
                ErrorCode.INTEGRITY, "Launch guard belongs to a different installation"
            )
        _release_ready(manager, guard)
        return LaunchRelease(target, guard)


def release_launch(
    settings: ClientSettings,
    installation: GameInstallation,
    plan: LaunchRelease,
    approval: str,
    *,
    queues_cleared: bool = False,
) -> dict[str, object]:
    if (
        approval != plan.fingerprint
        or not queues_cleared
        or plan.target != activation_target(installation)
    ):
        raise ModMeshError(
            ErrorCode.CONFLICT,
            "Approve this exact guard and confirm all queued launches are cleared",
        )
    with _activation_lease(settings, plan.target):
        manager = ProfileManager(create_game_registry(settings), installation)
        with manager._locked() as store:
            guard = launch_hold(store)
            if store is None or guard is None or guard != plan.guard:
                raise ModMeshError(ErrorCode.CONFLICT, "Launch guard changed; inspect it again")
            guard = read_launch_receipt(guard)
            _release_ready(manager, guard)
            save_launch_receipt(store, guard)
            audit: dict[str, object] = {
                "attempt_id": guard["attempt_id"],
                "target": plan.target,
                "released_at": datetime.now(UTC).isoformat(),
                "queues_cleared": True,
                "previous_state": guard["state"],
                "automatic_retry": False,
                "guard_digest": document_digest(guard),
            }
            with MetadataStore(settings.paths.database) as database:
                for _, record in store.list("launch.attempts"):
                    save_launch_receipt(database, read_launch_receipt(record))
                database.put("launch.releases", str(guard["attempt_id"]), audit)
            with store.transaction(write=True) as transaction:
                transaction.put("launch.releases", str(guard["attempt_id"]), audit)
                transaction.put(
                    "launch.guard",
                    "active",
                    {**guard, "state": "released", "outcome_state": guard["state"]},
                )
    return {
        "state": "released",
        "attempt_id": guard["attempt_id"],
        "pack_preserved": True,
        "automatic_retry": False,
    }
