from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from modmesh.core.cache import CacheEntry
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.hashing import verify_file
from modmesh.core.packs import PackEntry, closed
from modmesh.core.workspaces import document_digest
from modmesh.storage.installed import object_record, string_record
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class ArtifactProvenance:
    selection: PackEntry

    @property
    def identity(self) -> str:
        return document_digest(self.document())

    def document(self) -> dict[str, object]:
        mod = self.selection.mod
        return {
            "game_id": mod.project.game_id,
            "source_id": mod.project.reference.source_id,
            "project_id": mod.project.reference.project_id,
            "version_id": mod.version.version_id,
            "file_id": mod.file.file_id,
            "filename": mod.file.filename,
            "size": mod.file.size,
            "hashes": [
                {"algorithm": item.algorithm.value, "value": item.value}
                for item in sorted(mod.file.hashes, key=lambda item: item.algorithm.value)
            ],
            "acquisition": "user_local_import"
            if mod.project.reference.source_id == "local"
            else "provider_permission_required",
            "redistribution": "not_recorded",
        }

    @classmethod
    def read(cls, value: object, entry: PackEntry) -> ArtifactProvenance:
        result = cls(entry)
        data = closed(value, set(result.document()))
        if data != result.document():
            raise ModMeshError(
                ErrorCode.INTEGRITY, "Artifact provenance differs from locked selection"
            )
        return result


def record_verified_artifact(database: MetadataStore, entry: PackEntry, cached: CacheEntry) -> None:
    provenance = ArtifactProvenance(entry)
    if (cached.source_id, cached.file_id) != (
        entry.mod.file.project.source_id,
        entry.mod.file.file_id,
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Verified cache selection differs from provenance")
    actual = verify_file(cached.path, entry.mod.file.hashes, expected_size=entry.mod.file.size)
    if actual.sha256 != cached.artifact.sha256 or actual.size != cached.artifact.size:
        raise ModMeshError(ErrorCode.INTEGRITY, "Verified cache bytes changed before recording")
    with database.transaction(write=True) as transaction:
        prior = transaction.get("artifacts.provenance", provenance.identity)
        if prior is not None and prior.get("sha256") != actual.sha256.value:
            raise ModMeshError(
                ErrorCode.INTEGRITY, "Source reused an artifact identity for different bytes"
            )
        transaction.put(
            "artifacts.provenance",
            provenance.identity,
            {
                "selection": provenance.document(),
                "sha256": actual.sha256.value,
                "size": actual.size,
                "download_source_id": cached.download_source_id,
                "verified_at": datetime.now(UTC).isoformat(),
                "redistribution": "not_recorded",
            },
        )


def verified_provenance(
    database: MetadataStore, provenance: ArtifactProvenance
) -> dict[str, object] | None:
    record = database.get("artifacts.provenance", provenance.identity)
    if record is not None:
        if object_record(record.get("selection")) != provenance.document():
            raise ModMeshError(ErrorCode.INTEGRITY, "Stored artifact provenance was modified")
        from modmesh.domain.artifacts import FileHash, HashAlgorithm

        FileHash(HashAlgorithm.SHA256, string_record(record.get("sha256")))
    return record
