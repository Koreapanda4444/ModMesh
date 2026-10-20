from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import Connection, func, select
from sqlalchemy.engine import RowMapping

from modmesh.apps.server.auth import Account, require_owner
from modmesh.apps.server.database import Database
from modmesh.apps.server.schema import pack_versions, packs, users
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.packs import PackManifest


def unavailable() -> ModMeshError:
    return ModMeshError(ErrorCode.NOT_FOUND, "Pack is unavailable")


def pack_document(row: RowMapping) -> dict[str, object]:
    return {
        key: row[key]
        for key in (
            "id",
            "owner_id",
            "name",
            "description",
            "visibility",
            "latest_revision",
            "archived",
            "created_at",
            "updated_at",
        )
    }


class PackService:
    def __init__(self, database: Database, clock: Callable[[], float] = time.time) -> None:
        self.database, self.clock = database, clock

    def locked(self, connection: Connection, pack_id: str) -> RowMapping:
        row = (
            connection.execute(select(packs).where(packs.c.id == pack_id).with_for_update())
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise unavailable()
        return row

    def readable(self, row: RowMapping, viewer: Account | None) -> None:
        if viewer and viewer.user_id == row["owner_id"]:
            return
        if row["archived"] or row["visibility"] != "public":
            raise unavailable()

    def mine(self, owner: Account, limit: int, offset: int) -> dict[str, object]:
        with self.database.transaction() as connection:
            rows = (
                connection.execute(
                    select(packs)
                    .where(packs.c.owner_id == owner.user_id)
                    .order_by(packs.c.updated_at.desc(), packs.c.id)
                    .limit(limit + 1)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
            return {
                "packs": [pack_document(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    def publish(
        self, owner: Account, manifest: PackManifest, visibility: str, description: str
    ) -> dict[str, object]:
        now = int(self.clock())
        with self.database.transaction() as connection:
            connection.execute(
                select(users.c.id).where(users.c.id == owner.user_id).with_for_update()
            )
            existing = connection.execute(
                select(packs).where(packs.c.id == manifest.pack_id)
            ).first()
            if existing:
                raise ModMeshError(ErrorCode.CONFLICT, "Pack ID is already published")
            count = connection.execute(
                select(func.count()).select_from(packs).where(packs.c.owner_id == owner.user_id)
            ).scalar_one()
            if count >= 1000:
                raise ModMeshError(ErrorCode.RATE_LIMIT, "Published pack limit reached")
            connection.execute(
                packs.insert().values(
                    id=manifest.pack_id,
                    owner_id=owner.user_id,
                    name=manifest.name,
                    description=description,
                    visibility=visibility,
                    latest_revision=manifest.revision,
                    archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            self.insert_version(connection, manifest, now)
            return pack_document(self.locked(connection, manifest.pack_id))

    def insert_version(self, connection: Connection, manifest: PackManifest, now: int) -> None:
        connection.execute(
            pack_versions.insert().values(
                pack_id=manifest.pack_id,
                revision=manifest.revision,
                manifest_json=manifest.dumps().decode(),
                digest=manifest.digest,
                created_at=now,
            )
        )

    def append(
        self, owner: Account, pack_id: str, manifest: PackManifest, expected_revision: int
    ) -> dict[str, object]:
        with self.database.transaction() as connection:
            row = self.locked(connection, pack_id)
            require_owner(owner, str(row["owner_id"]))
            if row["archived"]:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Restore the pack before publishing a version"
                )
            if (
                manifest.pack_id != pack_id
                or manifest.revision != expected_revision + 1
                or row["latest_revision"] != expected_revision
            ):
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Pack revision changed; reload before publishing"
                )
            previous = connection.execute(
                select(pack_versions.c.manifest_json).where(
                    pack_versions.c.pack_id == pack_id,
                    pack_versions.c.revision == expected_revision,
                )
            ).scalar_one()
            old, new = PackManifest.loads(str(previous).encode()).context, manifest.context
            if (old.game_id, old.edition, old.platform) != (new.game_id, new.edition, new.platform):
                raise ModMeshError(ErrorCode.CONFLICT, "A different game needs a new pack")
            count = connection.execute(
                select(func.count())
                .select_from(pack_versions)
                .where(pack_versions.c.pack_id == pack_id)
            ).scalar_one()
            if count >= 1000:
                raise ModMeshError(ErrorCode.RATE_LIMIT, "Pack version limit reached")
            now = int(self.clock())
            self.insert_version(connection, manifest, now)
            connection.execute(
                packs.update()
                .where(packs.c.id == pack_id)
                .values(
                    name=manifest.name,
                    latest_revision=manifest.revision,
                    updated_at=now,
                )
            )
            return pack_document(self.locked(connection, pack_id))

    def edit(
        self, owner: Account, pack_id: str, visibility: str, description: str, archived: bool
    ) -> dict[str, object]:
        with self.database.transaction() as connection:
            row = self.locked(connection, pack_id)
            require_owner(owner, str(row["owner_id"]))
            connection.execute(
                packs.update()
                .where(packs.c.id == pack_id)
                .values(
                    visibility=visibility,
                    description=description,
                    archived=archived,
                    updated_at=int(self.clock()),
                )
            )
            return pack_document(self.locked(connection, pack_id))

    def get(self, pack_id: str, viewer: Account | None) -> dict[str, object]:
        with self.database.transaction() as connection:
            row = self.locked(connection, pack_id)
            self.readable(row, viewer)
            return pack_document(row)

    def versions(
        self, pack_id: str, viewer: Account | None, limit: int, offset: int
    ) -> dict[str, object]:
        with self.database.transaction() as connection:
            self.readable(self.locked(connection, pack_id), viewer)
            rows = (
                connection.execute(
                    select(
                        pack_versions.c.revision, pack_versions.c.digest, pack_versions.c.created_at
                    )
                    .where(pack_versions.c.pack_id == pack_id)
                    .order_by(pack_versions.c.revision.desc())
                    .limit(limit + 1)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
            return {
                "versions": [dict(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    def version(self, pack_id: str, revision: int, viewer: Account | None) -> dict[str, object]:
        with self.database.transaction() as connection:
            self.readable(self.locked(connection, pack_id), viewer)
            row = (
                connection.execute(
                    select(pack_versions).where(
                        pack_versions.c.pack_id == pack_id, pack_versions.c.revision == revision
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise unavailable()
            manifest = PackManifest.loads(str(row["manifest_json"]).encode())
            if manifest.digest != row["digest"]:
                raise ModMeshError(ErrorCode.INTEGRITY, "Stored manifest digest mismatch")
            return {
                "manifest": manifest.document(),
                "digest": manifest.digest,
                "created_at": row["created_at"],
            }
