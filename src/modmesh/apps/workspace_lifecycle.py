import os
from pathlib import Path

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_artifacts import local_artifact
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.lockfiles import PackLock, build_lock
from modmesh.core.packs import PackContext
from modmesh.core.rule_resources import builtin_registry
from modmesh.core.workspaces import WorkspaceStore
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore
from modmesh.storage.rules import load_rules


def freeze_workspace(
    settings: ClientSettings, pack_id: str, installation: GameInstallation, expected: str
) -> PackLock:
    with MetadataStore(settings.paths.database) as database:
        workspace = WorkspaceStore(database).get(pack_id)
        constraint = database.get("workspaces.constraints", pack_id)
    if workspace.digest != expected:
        raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed before lock preview")
    games = create_game_registry(settings)
    adapter = games.get(workspace.game_id)
    current = adapter.inspect(installation.path, installation.platform)
    if (
        current is None
        or current.version != installation.version
        or current.loader != installation.loader
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Game or loader changed; rediscover it")
    if constraint is not None and PackContext.read(
        constraint["context"]
    ) != PackContext.from_installation(current):
        raise ModMeshError(
            ErrorCode.CONFLICT, "Imported workspace requires its original game/build/loader context"
        )
    rules = (
        load_rules(settings.paths.database)
        if settings.paths.database.exists()
        else builtin_registry()
    )
    lock = build_lock(workspace, current, adapter, rules.get(workspace.game_id))
    for entry in lock.entries:
        if entry.mod.project.reference.source_id == "local":
            local_artifact(settings, entry.mod.file)
    return lock


def write_lock(path: Path, lock: PackLock) -> None:
    with path.open("xb") as stream:
        payload = lock.dumps()
        if stream.write(payload) != len(payload):
            raise ModMeshError(ErrorCode.STORAGE, "Incomplete lockfile export")
        stream.flush()
        os.fsync(stream.fileno())
