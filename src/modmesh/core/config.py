from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from modmesh.core.diagnostics import ErrorCode, ModMeshError, register_secret
from modmesh.domain.validation import require_id


class ConfigurationError(ModMeshError, ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.CONFIGURATION, message)


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
    cache_max_bytes: int = 5 * 1024**3
    source_priorities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_url", validate_server_url(self.server_url))
        _log_level(self.log_level)
        _cache_limit(self.cache_max_bytes)
        object.__setattr__(
            self, "source_priorities", MappingProxyType(_source_priorities(self.source_priorities))
        )


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8731
    environment: str = "development"
    database_url: str | None = field(default=None, repr=False)
    log_level: str = "INFO"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = field(default=None, repr=False)
    smtp_password: str | None = field(default=None, repr=False)
    smtp_from: str | None = None
    smtp_tls: bool = True
    session_seconds: int = 604800

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
        if self.environment == "production" and self.database_url is None:
            raise ConfigurationError("Production server requires PostgreSQL configuration")
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
        if type(self.smtp_port) is not int or not 1 <= self.smtp_port <= 65535:
            raise ConfigurationError("Invalid SMTP port")
        if type(self.session_seconds) is not int or not 300 <= self.session_seconds <= 2592000:
            raise ConfigurationError("Session duration must be 5 minutes to 30 days")
        if not isinstance(self.smtp_tls, bool):
            raise ConfigurationError("SMTP TLS must be a boolean")
        if self.smtp_host is not None and (
            not self.smtp_host or any(char.isspace() for char in self.smtp_host)
        ):
            raise ConfigurationError("Invalid SMTP hostname")
        if (
            self.smtp_host
            and not self.smtp_tls
            and self.smtp_host not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ConfigurationError("SMTP requires TLS except on loopback")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ConfigurationError("SMTP username and password must be configured together")


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
        path,
        "client",
        {
            "data_dir",
            "cache_dir",
            "cache_max_bytes",
            "server_url",
            "log_level",
            "source_priorities",
        },
        required=explicit,
    )
    for key in ("data_dir", "cache_dir", "cache_max_bytes", "server_url", "log_level"):
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
        cache_max_bytes=_cache_limit(data.get("cache_max_bytes", 5 * 1024**3)),
        source_priorities=_source_priorities(data.get("source_priorities", {})),
    )


def _cache_limit(value: object) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1024**5:
        raise ConfigurationError("Cache capacity must be an integer from 1 byte to 1 PiB")
    return value


def _source_priorities(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ConfigurationError("Source priorities must be a game-to-source-list table")
    result: dict[str, tuple[str, ...]] = {}
    for game_id, sources in value.items():
        if (
            not isinstance(game_id, str)
            or not isinstance(sources, (list, tuple))
            or any(not isinstance(source, str) for source in sources)
        ):
            raise ConfigurationError("Source priority entries must be string lists")
        try:
            require_id(game_id, "priority game ID")
            for source in sources:
                require_id(source, "priority source ID")
        except ModMeshError as exc:
            raise ConfigurationError("Invalid game/source ID in source priorities") from exc
        if len(set(sources)) != len(sources):
            raise ConfigurationError("Duplicate source in priority list")
        result[game_id] = tuple(sources)
    return result


def _secret(env: Mapping[str, str], name: str) -> str | None:
    value, filename = env.get(name) or None, env.get(name + "_FILE") or None
    if value and filename:
        raise ConfigurationError("Choose an environment secret or a secret file, not both")
    if filename:
        try:
            with Path(filename).open("rb") as stream:
                payload = stream.read(4097)
            if len(payload) > 4096:
                raise ValueError("Secret exceeds size limit")
            value = payload.decode("utf-8").rstrip("\r\n")
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Cannot read the configured secret file") from exc
        if not value or "\x00" in value:
            raise ConfigurationError("Invalid secret file contents")
    if value:
        register_secret(value)
    return value


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
            {
                "host",
                "port",
                "environment",
                "database_url",
                "log_level",
                "smtp_host",
                "smtp_port",
                "smtp_from",
                "smtp_tls",
                "session_seconds",
            },
            required=True,
        )
    for key, name in {
        "host": "MODMESH_BIND_HOST",
        "port": "MODMESH_BIND_PORT",
        "environment": "MODMESH_ENV",
        "database_url": "MODMESH_DATABASE_URL",
        "log_level": "MODMESH_LOG_LEVEL",
        "smtp_host": "MODMESH_SMTP_HOST",
        "smtp_port": "MODMESH_SMTP_PORT",
        "smtp_from": "MODMESH_SMTP_FROM",
        "smtp_tls": "MODMESH_SMTP_TLS",
        "session_seconds": "MODMESH_SESSION_SECONDS",
    }.items():
        if name in env and (env[name] or key not in {"smtp_host", "smtp_from"}):
            data[key] = env[name]
    port = data.get("port", 8731)
    if isinstance(port, bool) or not isinstance(port, (str, int)):
        raise ConfigurationError("Server port must be an integer")
    try:
        port = int(port)
    except ValueError:
        raise ConfigurationError("Server port must be an integer") from None
    database_url = _string(data, "database_url", None)
    if password := _secret(env, "MODMESH_DATABASE_PASSWORD"):
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            url = make_url(database_url or "")
            if url.get_backend_name() != "postgresql" or url.password is not None:
                raise ConfigurationError(
                    "Secret-file database URL needs PostgreSQL without a password"
                )
            database_url = url.set(password=password).render_as_string(hide_password=False)
        except ArgumentError as exc:
            raise ConfigurationError("Invalid database URL") from exc
    return ServerSettings(
        host=_string(data, "host", "127.0.0.1") or "127.0.0.1",
        port=port,
        environment=_string(data, "environment", "development") or "development",
        database_url=database_url,
        log_level=_string(data, "log_level", "INFO") or "INFO",
        smtp_host=_string(data, "smtp_host", None) or None,
        smtp_from=_string(data, "smtp_from", None) or None,
        smtp_username=env.get("MODMESH_SMTP_USERNAME") or None,
        smtp_password=_secret(env, "MODMESH_SMTP_PASSWORD"),
        smtp_port=_server_integer(data.get("smtp_port", 587)),
        smtp_tls=_server_boolean(data.get("smtp_tls", True)),
        session_seconds=_server_integer(data.get("session_seconds", 604800)),
    )


def _server_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigurationError("Server integer option is invalid")
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError("Server integer option is invalid") from exc


def _server_boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in {"true", "1"}:
        return True
    if isinstance(value, str) and value in {"false", "0"}:
        return False
    raise ConfigurationError("Server boolean option is invalid")
