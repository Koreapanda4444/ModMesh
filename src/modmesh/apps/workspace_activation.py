from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import partial

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_artifacts import artifact_vault
from modmesh.apps.workspace_legacy import activation_target, profile_configuration_digest
from modmesh.apps.workspace_lifecycle import freeze_workspace
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import _blocking
from modmesh.core.games import GameCapability
from modmesh.core.lockfiles import PackLock
from modmesh.core.planning import InstallInput
from modmesh.core.profiles import ProfileManager, ProfilePlan
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
        if profile is not None and profile_configuration_digest(profile) == binding.get(
            "profile_digest"
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
