from __future__ import annotations

from uuid import UUID

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.packs import PackManifest
from modmesh.remote.accounts import AccountClient


def pack_path(pack_id: str) -> str:
    return "/v1/packs/" + str(UUID(pack_id))


class PublishingClient:
    def __init__(self, account: AccountClient) -> None:
        self.account = account

    def mine(self, offset: int = 0) -> dict[str, object]:
        return self.account.request("GET", "/v1/account/packs", params={"offset": offset})

    def publish(
        self, manifest: PackManifest, visibility: str, description: str
    ) -> dict[str, object]:
        return self.account.request(
            "POST",
            "/v1/packs",
            {
                "manifest_json": manifest.dumps().decode(),
                "visibility": visibility,
                "description": description,
            },
        )

    def append(self, manifest: PackManifest, expected_revision: int) -> dict[str, object]:
        return self.account.request(
            "POST",
            pack_path(manifest.pack_id) + "/versions",
            {
                "manifest_json": manifest.dumps().decode(),
                "expected_revision": expected_revision,
            },
        )

    def edit(
        self, pack_id: str, visibility: str, description: str, archived: bool
    ) -> dict[str, object]:
        return self.account.request(
            "PATCH",
            pack_path(pack_id),
            {
                "visibility": visibility,
                "description": description,
                "archived": archived,
            },
        )

    def versions(self, pack_id: str, offset: int = 0) -> dict[str, object]:
        return self.account.request(
            "GET", pack_path(pack_id) + "/versions", params={"offset": offset}
        )

    def version(self, pack_id: str, revision: int) -> PackManifest:
        if not 1 <= revision < 2**31:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid pack revision")
        result = self.account.request("GET", pack_path(pack_id) + f"/versions/{revision}")
        manifest = PackManifest.read(result.get("manifest"))
        if (
            manifest.pack_id != str(UUID(pack_id))
            or manifest.revision != revision
            or manifest.digest != result.get("digest")
        ):
            raise ModMeshError(ErrorCode.INTEGRITY, "Published manifest identity mismatch")
        return manifest
