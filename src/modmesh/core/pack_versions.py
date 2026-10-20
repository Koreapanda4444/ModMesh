from __future__ import annotations

from dataclasses import dataclass

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.packs import PackManifest
from modmesh.core.profiles import ProfileManager
from modmesh.storage.installed import object_record


@dataclass(frozen=True)
class PackDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    updated: tuple[str, ...]
    settings: tuple[str, ...]
    context_changed: bool


def diff_manifests(previous: PackManifest, target: PackManifest) -> PackDiff:
    def entries(value: PackManifest) -> dict[str, object]:
        return {
            (
                item.mod.project.reference.source_id + ":" + item.mod.project.reference.project_id
            ): item.document()
            for item in value.entries
        }

    old, new = entries(previous), entries(target)
    before = {item.path: item.sha256 for item in previous.settings}
    after = {item.path: item.sha256 for item in target.settings}
    return PackDiff(
        tuple(sorted(new.keys() - old.keys())),
        tuple(sorted(old.keys() - new.keys())),
        tuple(sorted(key for key in old.keys() & new.keys() if old[key] != new[key])),
        tuple(
            sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
        ),
        previous.context != target.context,
    )


def save_version(manager: ProfileManager, manifest: PackManifest) -> None:
    manifest = PackManifest.loads(manifest.dumps())
    key = f"{manifest.pack_id}:{manifest.revision}"
    with manager._locked(create=True) as store:
        assert store is not None
        previous = store.get("packs.versions", key)
        if previous and previous.get("digest") != manifest.digest:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Local pack versions are immutable; increment the revision"
            )
        if previous is None:
            store.put(
                "packs.versions", key, {"digest": manifest.digest, "manifest": manifest.document()}
            )


def versions(manager: ProfileManager, pack_id: str) -> tuple[PackManifest, ...]:
    with manager._locked() as store:
        if store is None:
            return ()
        return tuple(
            sorted(
                (
                    PackManifest.read(value["manifest"])
                    for _, value in store.list("packs.versions")
                    if isinstance(value.get("manifest"), dict)
                    and object_record(value["manifest"]).get("pack_id") == pack_id
                ),
                key=lambda item: item.revision,
            )
        )
