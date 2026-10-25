from __future__ import annotations

import re
import secrets
from uuid import uuid4

from sqlalchemy import func, select

from modmesh.apps.server.auth import Account, digest, require_owner
from modmesh.apps.server.packs import PackService, pack_document, unavailable
from modmesh.apps.server.schema import pack_versions, share_codes
from modmesh.core.diagnostics import ErrorCode, ModMeshError, register_secret
from modmesh.core.packs import PackManifest


class SharingService(PackService):
    def issue(
        self, owner: Account, pack_id: str, revision: int | None, seconds: int, maximum: int | None
    ) -> dict[str, object]:
        now = int(self.clock())
        with self.database.transaction() as connection:
            pack = self.locked(connection, pack_id)
            require_owner(owner, str(pack["owner_id"]))
            if pack["archived"] or pack["visibility"] == "private":
                raise ModMeshError(ErrorCode.CONFLICT, "Select public or unlisted visibility first")
            if (
                revision is not None
                and not connection.execute(
                    select(pack_versions.c.revision).where(
                        pack_versions.c.pack_id == pack_id, pack_versions.c.revision == revision
                    )
                ).first()
            ):
                raise unavailable()
            count = connection.execute(
                select(func.count())
                .select_from(share_codes)
                .where(
                    share_codes.c.pack_id == pack_id,
                    share_codes.c.revoked.is_(False),
                    share_codes.c.expires_at > now,
                )
            ).scalar_one()
            if count >= 100:
                raise ModMeshError(ErrorCode.RATE_LIMIT, "Revoke unused share codes first")
            code, identity = "MM-" + secrets.token_urlsafe(18), str(uuid4())
            register_secret(code)
            connection.execute(
                share_codes.insert().values(
                    id=identity,
                    code_hash=digest(code),
                    pack_id=pack_id,
                    revision=revision,
                    expires_at=now + seconds,
                    max_uses=maximum,
                    uses=0,
                    revoked=False,
                    created_at=now,
                )
            )
            return {"id": identity, "code": code, "expires_at": now + seconds}

    def listing(self, owner: Account, pack_id: str, offset: int) -> dict[str, object]:
        with self.database.transaction() as connection:
            require_owner(owner, str(self.locked(connection, pack_id)["owner_id"]))
            rows = (
                connection.execute(
                    select(share_codes)
                    .where(share_codes.c.pack_id == pack_id)
                    .order_by(share_codes.c.created_at.desc(), share_codes.c.id)
                    .limit(51)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
            return {
                "shares": [{k: v for k, v in row.items() if k != "code_hash"} for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }

    def revoke(self, owner: Account, pack_id: str, identity: str) -> None:
        with self.database.transaction() as connection:
            require_owner(owner, str(self.locked(connection, pack_id)["owner_id"]))
            found = connection.execute(
                select(share_codes.c.id).where(
                    share_codes.c.id == identity, share_codes.c.pack_id == pack_id
                )
            ).first()
            if found is None:
                raise unavailable()
            connection.execute(
                share_codes.update().where(share_codes.c.id == identity).values(revoked=True)
            )

    def resolve(self, code: str) -> dict[str, object]:
        if not re.fullmatch(r"MM-[A-Za-z0-9_-]{24}", code):
            raise unavailable()
        hashed = digest(code)
        with self.database.transaction() as connection:
            identity = connection.execute(
                select(share_codes.c.pack_id).where(share_codes.c.code_hash == hashed)
            ).scalar_one_or_none()
            if identity is None:
                raise unavailable()
            pack = self.locked(connection, str(identity))
            row = (
                connection.execute(
                    select(share_codes).where(share_codes.c.code_hash == hashed).with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                pack["archived"]
                or pack["visibility"] == "private"
                or row["revoked"]
                or row["expires_at"] <= int(self.clock())
                or row["max_uses"] is not None
                and row["uses"] >= row["max_uses"]
            ):
                raise unavailable()
            revision = row["revision"] or pack["latest_revision"]
            version = (
                connection.execute(
                    select(pack_versions).where(
                        pack_versions.c.pack_id == identity, pack_versions.c.revision == revision
                    )
                )
                .mappings()
                .one()
            )
            manifest = PackManifest.loads(str(version["manifest_json"]).encode())
            if manifest.digest != version["digest"]:
                raise ModMeshError(ErrorCode.INTEGRITY, "Stored manifest digest mismatch")
            connection.execute(
                share_codes.update()
                .where(share_codes.c.id == row["id"])
                .values(uses=share_codes.c.uses + 1)
            )
            return {
                "pack": pack_document(pack),
                "manifest": manifest.document(),
                "digest": manifest.digest,
                "share_id": row["id"],
            }
