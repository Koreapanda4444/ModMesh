from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.launch_receipts import read_launch_receipt
from modmesh.core.workspaces import document_digest
from modmesh.storage.metadata import MetadataStore


def launch_hold(store: MetadataStore | None) -> dict[str, object] | None:
    record = store.get("launch.guard", "active") if store else None
    if record is not None:
        if record.get("state") == "released":
            previous = {key: value for key, value in record.items() if key != "outcome_state"}
            previous["state"] = record.get("outcome_state")
            previous = read_launch_receipt(previous)
            audit = store.get("launch.releases", str(previous["attempt_id"])) if store else None
            if (
                audit is None
                or audit.get("guard_digest") != document_digest(previous)
                or audit.get("queues_cleared") is not True
            ):
                raise ModMeshError(ErrorCode.STORAGE, "Launch release audit is missing or invalid")
        else:
            read_launch_receipt(record)
    if record is not None and record.get("state") not in {"released", "failed", "manual_required"}:
        return record
    return None


def require_launch_idle(store: MetadataStore | None) -> None:
    if launch_hold(store) is not None:
        raise ModMeshError(
            ErrorCode.CONFLICT,
            "Launch is pending or handed off; close the game and clear queued launcher requests, "
            "then explicitly release the launch guard before changing files or retrying",
        )
