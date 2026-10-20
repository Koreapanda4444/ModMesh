from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from pydantic import Field

from modmesh.apps.server.auth import Account
from modmesh.apps.server.auth_routes import Input, account, authentication
from modmesh.apps.server.packs import PackService
from modmesh.core.packs import MAX_MANIFEST_BYTES, PackManifest

router = APIRouter(prefix="/v1")
Visibility = Literal["public", "unlisted", "private"]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]
Revision = Annotated[int, Field(ge=1, le=2**31 - 1)]


class PublishInput(Input):
    manifest_json: Annotated[str, Field(min_length=2, max_length=MAX_MANIFEST_BYTES)]
    visibility: Visibility = "private"
    description: Annotated[str, Field(max_length=2000)] = ""


class VersionInput(Input):
    manifest_json: Annotated[str, Field(min_length=2, max_length=MAX_MANIFEST_BYTES)]
    expected_revision: Revision


class EditInput(Input):
    visibility: Visibility
    description: Annotated[str, Field(max_length=2000)]
    archived: bool


def service(request: Request) -> PackService:
    return PackService(authentication(request).database)


def viewer(request: Request) -> Account | None:
    return account(request) if request.headers.get("authorization") else None


def writer(request: Request) -> Account:
    owner = account(request)
    authentication(request).limit("pack-write", owner.user_id, 60)
    return owner


@router.get("/account/packs")
def my_packs(request: Request, limit: Limit = 50, offset: Offset = 0) -> dict[str, object]:
    return service(request).mine(account(request), limit, offset)


@router.post("/packs", status_code=201)
def publish(body: PublishInput, request: Request) -> dict[str, object]:
    owner = writer(request)
    manifest = PackManifest.loads(body.manifest_json.encode())
    return service(request).publish(owner, manifest, body.visibility, body.description)


@router.get("/packs/{pack_id}")
def get_pack(pack_id: UUID, request: Request) -> dict[str, object]:
    return service(request).get(str(pack_id), viewer(request))


@router.patch("/packs/{pack_id}")
def edit_pack(pack_id: UUID, body: EditInput, request: Request) -> dict[str, object]:
    return service(request).edit(
        writer(request), str(pack_id), body.visibility, body.description, body.archived
    )


@router.post("/packs/{pack_id}/versions", status_code=201)
def add_version(pack_id: UUID, body: VersionInput, request: Request) -> dict[str, object]:
    owner = writer(request)
    manifest = PackManifest.loads(body.manifest_json.encode())
    return service(request).append(owner, str(pack_id), manifest, body.expected_revision)


@router.get("/packs/{pack_id}/versions")
def versions(
    pack_id: UUID, request: Request, limit: Limit = 50, offset: Offset = 0
) -> dict[str, object]:
    return service(request).versions(str(pack_id), viewer(request), limit, offset)


@router.get("/packs/{pack_id}/versions/{revision}")
def version(
    pack_id: UUID, revision: Annotated[int, Path(ge=1, le=2**31 - 1)], request: Request
) -> dict[str, object]:
    return service(request).version(str(pack_id), revision, viewer(request))
