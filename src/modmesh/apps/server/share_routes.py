from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from modmesh.apps.server.auth_routes import Input, authentication, peer
from modmesh.apps.server.pack_routes import Offset, Revision, writer
from modmesh.apps.server.sharing import SharingService

router = APIRouter(prefix="/v1")


class ShareInput(Input):
    revision: Revision | None = None
    expires_in: Annotated[int, Field(ge=60, le=2592000)] = 604800
    max_uses: Annotated[int, Field(ge=1, le=1000000)] | None = None


class ResolveInput(Input):
    code: Annotated[str, Field(min_length=1, max_length=128)]


def service(request: Request) -> SharingService:
    return SharingService(authentication(request).database)


@router.post("/packs/{pack_id}/shares", status_code=201)
def issue(pack_id: UUID, body: ShareInput, request: Request) -> dict[str, object]:
    return service(request).issue(
        writer(request), str(pack_id), body.revision, body.expires_in, body.max_uses
    )


@router.get("/packs/{pack_id}/shares")
def listing(pack_id: UUID, request: Request, offset: Offset = 0) -> dict[str, object]:
    return service(request).listing(writer(request), str(pack_id), offset)


@router.delete("/packs/{pack_id}/shares/{share_id}")
def revoke(pack_id: UUID, share_id: UUID, request: Request) -> dict[str, str]:
    service(request).revoke(writer(request), str(pack_id), str(share_id))
    return {"status": "revoked"}


@router.post("/shares/resolve")
def resolve(body: ResolveInput, request: Request) -> dict[str, object]:
    authentication(request).limit("share-resolve", peer(request), 60)
    return service(request).resolve(body.code)
