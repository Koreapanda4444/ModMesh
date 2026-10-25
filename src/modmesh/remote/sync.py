from __future__ import annotations

import hashlib
from dataclasses import dataclass

from modmesh.core.config import validate_server_url
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.pack_versions import PackDiff, diff_manifests
from modmesh.core.packs import PackManifest
from modmesh.remote.accounts import AccountClient
from modmesh.remote.packs import PublishingClient, pack_path
from modmesh.storage.installed import object_record
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class RemoteSnapshot:
    server: str
    manifest: PackManifest
    previous: PackManifest | None
    diff: PackDiff | None


class RemotePacks:
    def __init__(self, client: AccountClient, store: MetadataStore) -> None:
        self.client, self.store = client, store
        self.scope = hashlib.sha256(client.server.encode()).hexdigest()

    def receipt(self, local_key: str) -> PackManifest | None:
        data = self.store.get("remote.applied", self.scope + ":" + local_key)
        return PackManifest.read(data["manifest"]) if data else None

    def fetch(
        self, code: str, *, previous: PackManifest | None = None, authenticated: bool = False
    ) -> RemoteSnapshot:
        if code:
            result = PublishingClient(self.client).resolve_share(code)
            manifest = PackManifest.read(result.get("manifest"))
            if object_record(result.get("pack")).get("id") != manifest.pack_id:
                raise ModMeshError(ErrorCode.INTEGRITY, "Shared pack identity mismatch")
        elif previous:
            pack = self.client.request(
                "GET", pack_path(previous.pack_id), authenticated=authenticated
            )
            revision = pack.get("latest_revision")
            if type(revision) is not int or not 1 <= revision < 2**31:
                raise ModMeshError(ErrorCode.INTEGRITY, "Invalid remote revision")
            result = self.client.request(
                "GET",
                pack_path(previous.pack_id) + f"/versions/{revision}",
                authenticated=authenticated,
            )
            manifest = PackManifest.read(result.get("manifest"))
            if manifest.revision != revision or manifest.pack_id != previous.pack_id:
                raise ModMeshError(ErrorCode.INTEGRITY, "Remote pack identity mismatch")
        else:
            raise ModMeshError(ErrorCode.VALIDATION, "Enter a share code first")
        if manifest.digest != result.get("digest"):
            raise ModMeshError(ErrorCode.INTEGRITY, "Remote manifest digest mismatch")
        manifest = PackManifest.loads(manifest.dumps())
        if previous and (
            previous.pack_id != manifest.pack_id
            or manifest.revision < previous.revision
            or manifest.revision == previous.revision
            and manifest.digest != previous.digest
        ):
            raise ModMeshError(
                ErrorCode.CONFLICT, "Remote pack changed identity or rewrote history"
            )
        key = f"{self.scope}:{manifest.pack_id}:{manifest.revision}"
        old = self.store.get("remote.versions", key)
        if old and old.get("digest") != manifest.digest:
            raise ModMeshError(ErrorCode.CONFLICT, "Remote revision content changed")
        self.store.put("remote.versions", key, {"digest": manifest.digest})
        return RemoteSnapshot(
            self.client.server,
            manifest,
            previous,
            diff_manifests(previous, manifest) if previous else None,
        )

    def applied(self, local_key: str, snapshot: RemoteSnapshot) -> None:
        if validate_server_url(snapshot.server) != self.client.server:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Server changed before recording the applied pack"
            )
        self.store.put(
            "remote.applied",
            self.scope + ":" + local_key,
            {
                "server": snapshot.server,
                "manifest": snapshot.manifest.document(),
                "digest": snapshot.manifest.digest,
            },
        )
