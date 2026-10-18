import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from modmesh.core.diagnostics import ErrorCode, ModMeshError


def require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ModMeshError(ErrorCode.VALIDATION, f"{field_name} must be nonempty text")


def require_id(value: str, field_name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise ModMeshError(ErrorCode.VALIDATION, f"Invalid {field_name}")


def relative_path(value: str) -> str:
    require_text(value, "relative path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts or str(path) == ".":
        raise ModMeshError(ErrorCode.VALIDATION, "File path must remain within the installation")
    return str(path)


def validate_web_url(value: str | None) -> None:
    if value is None:
        return
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid web URL")
