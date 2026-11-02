import math
from datetime import datetime
from uuid import UUID

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.launching import LaunchCallState, LaunchMethod
from modmesh.core.workspace_receipts import ActivationReceipt
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.storage.installed import object_record, string_record
from modmesh.storage.metadata import MetadataStore


def read_launch_receipt(value: object) -> dict[str, object]:
    data = object_record(value)
    try:
        attempt = string_record(data.get("attempt_id"))
        target = string_record(data.get("target"))
        receipt = ActivationReceipt.read(data.get("activation"))
        if (
            str(UUID(attempt)) != attempt
            or str(UUID(target)) != target
            or receipt.target != target
            or data.get("schema_version") != 1
            or data.get("state") not in {"invoking", *(item.value for item in LaunchCallState)}
            or data.get("method") not in {item.value for item in LaunchMethod}
            or data.get("mod_loading") != "unverified"
            or data.get("game_process_observed") is not False
        ):
            raise ValueError
        for key in ("created_at", "updated_at"):
            if key == "updated_at" and key not in data:
                continue
            if datetime.fromisoformat(string_record(data.get(key))).tzinfo is None:
                raise ValueError
        pid, started = data.get("pid"), data.get("process_started_at")
        if pid is not None and (type(pid) is not int or pid < 1):
            raise ValueError
        if started is not None and (
            not isinstance(started, (float, int))
            or isinstance(started, bool)
            or not math.isfinite(started)
            or started <= 0
        ):
            raise ValueError
        if not isinstance(data.get("detail", ""), str) or len(str(data.get("detail", ""))) > 4096:
            raise ValueError
        FileHash(HashAlgorithm.SHA256, string_record(data.get("launch_fingerprint")))
    except (ValueError, TypeError) as exc:
        raise ModMeshError(ErrorCode.STORAGE, "Invalid launch receipt") from exc
    return data


def save_launch_receipt(database: MetadataStore, record: dict[str, object]) -> None:
    record = read_launch_receipt(record)
    identity = string_record(record["attempt_id"])
    with database.transaction(write=True) as transaction:
        previous = transaction.get("launch.attempts", identity)
        if previous is not None:
            previous = read_launch_receipt(previous)
            for key in (
                "attempt_id",
                "target",
                "activation",
                "launch_fingerprint",
                "method",
                "created_at",
            ):
                if previous[key] != record[key]:
                    raise ModMeshError(ErrorCode.INTEGRITY, "Launch receipt identity changed")
            if previous["state"] != "invoking" and previous != record:
                raise ModMeshError(ErrorCode.CONFLICT, "Completed launch receipts are immutable")
        transaction.put("launch.attempts", identity, record)
