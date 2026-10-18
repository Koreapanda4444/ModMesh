"""Strict helpers at the untrusted API JSON / domain model boundary."""

from modmesh.core.diagnostics import ErrorCode, ModMeshError


def object_data(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ModMeshError(ErrorCode.NETWORK, "Source API object has an unexpected format")
    return dict(value)


def array_data(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ModMeshError(ErrorCode.NETWORK, "Source API list has an unexpected format")
    return list(value)


def text(data: dict[str, object], key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ModMeshError(ErrorCode.NETWORK, f"Source API field {key} must be text")
    return value


def optional_text(data: dict[str, object], key: str) -> str | None:
    return None if data.get(key) is None else text(data, key)


def integer(data: dict[str, object], key: str, default: int | None = None) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ModMeshError(ErrorCode.NETWORK, f"Source API field {key} must be an integer")
    return value
