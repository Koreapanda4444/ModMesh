"""Validated TOML/environment configuration with no network or write side effects."""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """Configuration is invalid; messages must never contain secret values."""


def validate_server_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ConfigurationError("Invalid server URL") from exc
    if (
        url.scheme not in {"https", "http"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(c.isspace() for c in value)
        or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})
        or port == 0
    ):
        raise ConfigurationError("Server URL requires HTTPS, or HTTP on a loopback host")
    return urlunsplit((url.scheme, url.netloc.lower(), url.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @classmethod
    def defaults(cls, env: Mapping[str, str], platform: str | None = None) -> AppPaths:
        home = Path.home()
        platform = platform or sys.platform
        if platform == "win32":
            config = Path(env.get("APPDATA", str(home / "AppData/Roaming"))) / "ModMesh"
            data = Path(env.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "ModMesh"
            cache = data / "cache"
        elif platform == "darwin":
            config = data = home / "Library/Application Support/ModMesh"
            cache = home / "Library/Caches/ModMesh"
        else:
            config = Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "modmesh"
            data = Path(env.get("XDG_DATA_HOME", str(home / ".local/share"))) / "modmesh"
            cache = Path(env.get("XDG_CACHE_HOME", str(home / ".cache"))) / "modmesh"
        return cls(config.resolve(), data.resolve(), cache.resolve())

    @property
    def database(self) -> Path:
        return self.data_dir / "metadata.sqlite3"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"


@dataclass(frozen=True)
class ClientSettings:
    paths: AppPaths
    server_url: str | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_url", validate_server_url(self.server_url))
        _log_level(self.log_level)


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8731
    environment: str = "development"
    database_url: str | None = field(default=None, repr=False)
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.host or any(c.isspace() for c in self.host):
            raise ConfigurationError("Invalid server bind host")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ConfigurationError("Server port must be between 1 and 65535")
        if self.environment not in {"development", "production"}:
            raise ConfigurationError("Environment must be development or production")
        if self.database_url is not None:
            from sqlalchemy.engine import make_url
            from sqlalchemy.exc import ArgumentError

            try:
                url = make_url(self.database_url)
            except ArgumentError as exc:
                raise ConfigurationError("Invalid database URL") from exc
            if url.get_backend_name() not in {"postgresql", "sqlite"}:
                raise ConfigurationError("Unsupported database backend")
            if self.environment == "production" and url.get_backend_name() != "postgresql":
                raise ConfigurationError("Production server requires PostgreSQL")
        _log_level(self.log_level)


def _log_level(value: str) -> str:
    if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("Invalid log level")
    return value


def _section(path: Path, name: str, allowed: set[str], *, required: bool) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        if not required:
            return {}
        raise ConfigurationError("Specified configuration file does not exist") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError("Cannot read configuration TOML") from exc
    if set(document) - {"client", "server"}:
        raise ConfigurationError("Unknown configuration section")
    section = document.get(name, {})
    if not isinstance(section, dict) or set(section) - allowed:
        raise ConfigurationError(f"Unknown or invalid {name} configuration fields")
    return dict(section)


def _string(data: Mapping[str, object], key: str, default: str | None) -> str | None:
    value = data.get(key, default)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ConfigurationError(f"{key} must be a nonempty string")
    return value


def load_client_settings(
    path: Path | None = None, *, env: Mapping[str, str] | None = None
) -> ClientSettings:
    env = os.environ if env is None else env
    paths = AppPaths.defaults(env)
    explicit = path is not None or "MODMESH_CONFIG" in env
    path = path or Path(env.get("MODMESH_CONFIG", str(paths.config_dir / "config.toml")))
    data = _section(
        path, "client", {"data_dir", "cache_dir", "server_url", "log_level"}, required=explicit
    )
    for key in ("data_dir", "cache_dir", "server_url", "log_level"):
        if (name := f"MODMESH_{key.upper()}") in env:
            data[key] = env[name]

    def directory(key: str, default: Path) -> Path:
        value = _string(data, key, None)
        if value is None:
            return default
        result = Path(value).expanduser()
        return (result if result.is_absolute() else path.parent / result).resolve()

    return ClientSettings(
        AppPaths(
            paths.config_dir,
            directory("data_dir", paths.data_dir),
            directory("cache_dir", paths.cache_dir),
        ),
        server_url=_string(data, "server_url", None),
        log_level=_log_level(_string(data, "log_level", "INFO") or "INFO"),
    )


def load_server_settings(
    path: Path | None = None, *, env: Mapping[str, str] | None = None
) -> ServerSettings:
    env = os.environ if env is None else env
    data: dict[str, object] = {}
    if path is not None or "MODMESH_SERVER_CONFIG" in env:
        path = path or Path(env["MODMESH_SERVER_CONFIG"])
        data = _section(
            path,
            "server",
            {"host", "port", "environment", "database_url", "log_level"},
            required=True,
        )
    for key, name in {
        "host": "MODMESH_BIND_HOST",
        "port": "MODMESH_BIND_PORT",
        "environment": "MODMESH_ENV",
        "database_url": "MODMESH_DATABASE_URL",
        "log_level": "MODMESH_LOG_LEVEL",
    }.items():
        if name in env:
            data[key] = env[name]
    port = data.get("port", 8731)
    if isinstance(port, bool) or not isinstance(port, (str, int)):
        raise ConfigurationError("Server port must be an integer")
    try:
        port = int(port)
    except ValueError:
        raise ConfigurationError("Server port must be an integer") from None
    return ServerSettings(
        host=_string(data, "host", "127.0.0.1") or "127.0.0.1",
        port=port,
        environment=_string(data, "environment", "development") or "development",
        database_url=_string(data, "database_url", None),
        log_level=_string(data, "log_level", "INFO") or "INFO",
    )
