from modmesh.apps.runtime import create_game_registry
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.pack_io import export_profile
from modmesh.core.packs import PackManifest
from modmesh.core.profiles import Profile, ProfileManager, profile_record
from modmesh.core.workspaces import Workspace, WorkspaceStore, document_digest, pack_name
from modmesh.domain.models import GameInstallation
from modmesh.domain.workspaces import ActivationTarget, PackIdentity, installation_identity
from modmesh.storage.installed import array_record, object_record
from modmesh.storage.metadata import MetadataStore


def profile_configuration_digest(profile: Profile) -> str:
    record = profile_record(profile)
    record.pop("profile_id")
    record.pop("name")
    record["mods"] = [
        {key: value for key, value in object_record(raw).items() if key != "operation_id"}
        for raw in array_record(record["mods"])
    ]
    return document_digest(record)


def activation_target(installation: GameInstallation) -> str:
    return ActivationTarget(
        installation_identity(installation.game_id, installation.path, installation.platform)
    ).target_id


def import_manifest_workspace(settings: ClientSettings, manifest: PackManifest) -> Workspace:
    manifest = PackManifest.loads(manifest.dumps())
    create_game_registry(settings).get(manifest.context.game_id)
    source = "manifest:" + manifest.digest
    with (
        MetadataStore(settings.paths.database) as database,
        database.transaction(write=True) as transaction,
    ):
        linked = transaction.get("workspaces.legacy", source)
        if linked is not None:
            existing = transaction.get("workspaces.items", str(linked["pack_id"]))
            if existing is None:
                raise ModMeshError(
                    ErrorCode.CONFLICT,
                    "Imported workspace was archived; original manifest retained",
                )
            return Workspace.read(existing)
        identity = PackIdentity.create(manifest.context.game_id)
        workspace = Workspace.read(
            Workspace(
                identity.pack_id,
                identity.game_id,
                pack_name(manifest.name),
                entries=manifest.entries,
                settings=manifest.settings,
            ).document()
        )
        transaction.put("workspaces.items", workspace.pack_id, workspace.document())
        transaction.put(
            "workspaces.constraints",
            workspace.pack_id,
            {"context": manifest.context.document()},
        )
        transaction.put(
            "workspaces.legacy",
            source,
            {"pack_id": workspace.pack_id, "manifest_digest": manifest.digest},
        )
        return workspace


def adopt_profile(
    settings: ClientSettings,
    installation: GameInstallation,
    profile_id: str,
    *,
    name: str | None = None,
) -> Workspace:
    target = activation_target(installation)
    source = f"profile:{target}:{profile_id}"
    with MetadataStore(settings.paths.database) as database:
        linked = database.get("workspaces.legacy", source)
        if linked is not None:
            return WorkspaceStore(database).get(str(linked["pack_id"]))
    manager = ProfileManager(create_game_registry(settings), installation)
    original = next((item for item in manager.profiles() if item.profile_id == profile_id), None)
    if original is None:
        raise ModMeshError(ErrorCode.NOT_FOUND, "Legacy profile does not exist")
    copied = manager.create(pack_name(name or original.name), clone=profile_id)
    manifest = export_profile(manager, copied.profile_id)
    workspace = import_manifest_workspace(settings, manifest)
    with (
        MetadataStore(settings.paths.database) as database,
        database.transaction(write=True) as transaction,
    ):
        linked = transaction.get("workspaces.legacy", source)
        if linked is not None:
            return WorkspaceStore(database).get(str(linked["pack_id"]))
        transaction.put(
            "workspaces.profiles",
            f"{target}:{workspace.pack_id}",
            {
                "pack_id": workspace.pack_id,
                "target": target,
                "workspace_digest": workspace.digest,
                "selection_digest": workspace.selection_digest,
                "profile_id": copied.profile_id,
                "profile_digest": profile_configuration_digest(copied),
                "origin": "legacy",
            },
        )
        transaction.put(
            "workspaces.legacy",
            source,
            {"pack_id": workspace.pack_id, "profile_id": profile_id},
        )
    return workspace
