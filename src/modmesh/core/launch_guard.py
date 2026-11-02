from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.storage.metadata import MetadataStore


def launch_hold(store: MetadataStore | None) -> dict[str, object] | None:
    record = store.get("launch.guard", "active") if store else None
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
