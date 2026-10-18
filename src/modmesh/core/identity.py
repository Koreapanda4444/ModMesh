import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.models import SourceRef
from modmesh.domain.validation import require_id, require_text
from modmesh.storage.metadata import MetadataStore


class EvidenceKind(StrEnum):
    NATIVE_ID = "native_id"
    PLATFORM_MAPPING = "platform_mapping"
    AUTHOR_REFERENCE = "author_reference"
    USER_CONFIRMATION = "user_confirmation"


@dataclass(frozen=True)
class IdentityEvidence:
    kind: EvidenceKind
    reference: str

    def __post_init__(self) -> None:
        require_text(self.reference, "identity evidence reference")


def _key(game_id: str, reference: SourceRef) -> str:
    require_id(game_id, "game ID")
    return json.dumps([game_id, reference.source_id, reference.project_id], separators=(",", ":"))


class IdentityRegistry:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def resolve(self, game_id: str, reference: SourceRef) -> str | None:
        binding = self.store.get("identity.bindings", _key(game_id, reference))
        return str(binding["canonical_id"]) if binding is not None else None

    def get_or_create(self, game_id: str, reference: SourceRef) -> str:
        key = _key(game_id, reference)
        with self.store.transaction(write=True) as transaction:
            existing = transaction.get("identity.bindings", key)
            if existing is not None:
                return str(existing["canonical_id"])
            canonical_id = str(uuid5(NAMESPACE_URL, "modmesh:canonical:" + key))
            transaction.put("identity.groups", canonical_id, {"game_id": game_id})
            transaction.put(
                "identity.bindings",
                key,
                {
                    "canonical_id": canonical_id,
                    "game_id": game_id,
                    "source_id": reference.source_id,
                    "project_id": reference.project_id,
                    "evidence_kind": EvidenceKind.NATIVE_ID.value,
                    "evidence_reference": key,
                },
            )
            return canonical_id

    def bind(
        self, canonical_id: str, game_id: str, reference: SourceRef, evidence: IdentityEvidence
    ) -> None:
        require_text(canonical_id, "canonical ID")
        if evidence.kind == EvidenceKind.NATIVE_ID:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Cross-source bindings require explicit evidence"
            )
        key = _key(game_id, reference)
        with self.store.transaction(write=True) as transaction:
            group = transaction.get("identity.groups", canonical_id)
            if group is None:
                raise ModMeshError(ErrorCode.NOT_FOUND, "Unknown canonical mod ID")
            if group["game_id"] != game_id:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Cannot bind projects across different games"
                )
            existing = transaction.get("identity.bindings", key)
            if existing is not None and existing["canonical_id"] != canonical_id:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Project is already bound to another canonical ID"
                )
            transaction.put(
                "identity.bindings",
                key,
                {
                    "canonical_id": canonical_id,
                    "game_id": game_id,
                    "source_id": reference.source_id,
                    "project_id": reference.project_id,
                    "evidence_kind": evidence.kind.value,
                    "evidence_reference": evidence.reference,
                },
            )

    def unbind(self, game_id: str, reference: SourceRef) -> bool:
        with self.store.transaction(write=True) as transaction:
            return transaction.delete("identity.bindings", _key(game_id, reference))

    def projects(self, canonical_id: str) -> tuple[SourceRef, ...]:
        return tuple(
            sorted(
                SourceRef(str(value["source_id"]), str(value["project_id"]))
                for _, value in self.store.list("identity.bindings")
                if value["canonical_id"] == canonical_id
            )
        )
