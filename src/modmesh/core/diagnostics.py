from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit, urlunsplit


class SupportState(StrEnum):
    AVAILABLE = "available"
    UNVERIFIED = "unverified"
    WARNING = "warning"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    state: SupportState
    user_message: str
    detail: str

    @classmethod
    def environment(cls, code: str, message: str, blocking: bool = False) -> Diagnostic:
        state = SupportState.BLOCKED if blocking else SupportState.WARNING
        if not blocking and any(word in code for word in ("unknown", "unverified", "missing")):
            state = SupportState.UNVERIFIED
        if not blocking and code in {"steam_managed", "manual_handoff"}:
            state = SupportState.HANDOFF
        messages = {
            "invalid_game_rule": "저장된 게임 규칙을 거부했습니다. 원본 규칙과 백업을 확인하세요.",
            "unknown_version": "게임 버전을 확인하지 못했습니다.",
            "unverified_steam_build": "Steam 빌드를 확인하지 못했습니다.",
            "steam_managed": "구독·업데이트·제거는 Steam에서 진행하세요.",
            "game_running": "게임을 종료한 뒤 파일 변경을 진행하세요.",
            "process_unreadable": "게임 프로세스를 확인할 수 없습니다. 게임을 종료하세요.",
            "unsupported_platform": "현재 운영체제에서 지원하지 않는 게임입니다.",
            "missing_path": "등록한 게임 폴더를 찾을 수 없습니다.",
            "manual_path_invalid": "등록한 게임 경로를 다시 확인하세요.",
        }
        return cls(
            code,
            state,
            messages.get(
                code, "게임 환경을 확인해야 합니다." if blocking else "추가 확인이 필요합니다."
            ),
            redact_text(message),
        )


class ErrorCode(StrEnum):
    CONFIGURATION = "configuration"
    VALIDATION = "validation"
    STORAGE = "storage"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    INTEGRITY = "integrity"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"


_SENSITIVE = re.compile(
    r"token|password|passwd|authorization|cookie|secret|apikey|credential|sessionid"
)
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")
_HEADER = re.compile(r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]*")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[-_]?key|access[-_]?token|refresh[-_]?token|password|passwd|secret)"
    r"([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_secrets: set[str] = set()
_secret_lock = threading.Lock()


def register_secret(value: str) -> None:
    if len(value) >= 4:
        with _secret_lock:
            _secrets.add(value)


def redact_text(value: str) -> str:
    def clean_url(match: re.Match[str]) -> str:
        try:
            url = urlsplit(match.group())
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "[REDACTED]" if url.query else "", ""))
        except ValueError:
            return "[REDACTED URL]"

    with _secret_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    value = _URL.sub(clean_url, value)
    value = _HEADER.sub(lambda m: f"{m.group(1)}: [REDACTED]", value)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)


def redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if _SENSITIVE.search(re.sub(r"[-_]", "", str(key)).lower())
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_text(str(value))


class ModMeshError(Exception):
    def __init__(
        self, code: ErrorCode, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        self.code = code
        self.message = redact_text(message)
        self.details = dict(details or {})
        super().__init__(self.message)

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code.value, "message": self.message, "details": redact(self.details)}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        context = getattr(record, "context", None)
        if context is not None:
            data["context"] = redact(context)
        if record.exc_info:
            data["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(data, ensure_ascii=False)


def configure_logging(*, level: str, directory: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("modmesh")
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handlers.append(
            RotatingFileHandler(
                directory / "modmesh.log", maxBytes=2_097_152, backupCount=3, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def install_exception_hook(logger: logging.Logger) -> None:
    def report(
        kind: type[BaseException], error: BaseException, traceback: TracebackType | None
    ) -> None:
        if issubclass(kind, KeyboardInterrupt):
            return
        logger.critical("Unhandled application error", exc_info=(kind, error, traceback))

    sys.excepthook = report
