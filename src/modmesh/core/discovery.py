from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError, redact_text
from modmesh.core.games import (
    DiscoveryContext,
    EnvironmentIssue,
    EnvironmentReport,
    GameAdapter,
    GameRegistry,
)
from modmesh.domain.models import GameInstallation, Platform
from modmesh.domain.workspaces import installation_identity
from modmesh.storage.metadata import MetadataStore


def current_platform() -> Platform:
    return {"win32": Platform.WINDOWS, "darwin": Platform.MACOS}.get(sys.platform, Platform.LINUX)


def _path_key(path: Path, platform: Platform) -> str:
    value = str(path.resolve())
    return value.casefold() if platform == Platform.WINDOWS else value


def manual_path_record(
    saved: dict[str, object] | None, path: Path, platform: Platform
) -> dict[str, object]:
    raw = (saved or {}).get("paths", [])
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise ModMeshError(ErrorCode.STORAGE, "Invalid saved game path configuration")
    paths = {_path_key(Path(str(value)), platform): str(value) for value in raw}
    paths[_path_key(path, platform)] = str(path)
    return {"paths": sorted(paths.values())}


@dataclass(frozen=True)
class DiscoveryRecord:
    installation: GameInstallation
    validation: EnvironmentReport
    origin: str


@dataclass(frozen=True)
class DiscoveryResult:
    records: tuple[DiscoveryRecord, ...]
    issues: tuple[EnvironmentIssue, ...]
    registered_games: tuple[str, ...]


class DiscoveryEngine:
    def __init__(self, registry: GameRegistry, store: MetadataStore) -> None:
        self.registry = registry
        self.store = store

    def manual_paths(self, game_id: str) -> tuple[Path, ...]:
        self.registry.get(game_id)
        value = self.store.get("discovery.manual", game_id)
        if value is None:
            return ()
        paths = value.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ModMeshError(ErrorCode.STORAGE, "Invalid saved game path configuration")
        return tuple(Path(path) for path in paths)

    def register_manual(
        self, game_id: str, path: Path, context: DiscoveryContext
    ) -> GameInstallation:
        adapter = self.registry.get(game_id)
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise ModMeshError(ErrorCode.NOT_FOUND, "Manual game directory does not exist")
        installation = adapter.inspect(path, context.platform)
        if installation is None or installation.game_id != game_id:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Manual path is not a recognized installation of this game"
            )
        report = adapter.validate(installation)
        if not report.valid:
            raise ModMeshError(
                ErrorCode.VALIDATION,
                "Manual game environment validation failed",
                details={"issues": [asdict(issue) for issue in report.issues]},
            )
        with self.store.transaction(write=True) as transaction:
            transaction.put(
                "discovery.manual",
                game_id,
                manual_path_record(
                    transaction.get("discovery.manual", game_id), path, context.platform
                ),
            )
        return installation

    def unregister_manual(self, game_id: str, path: Path, context: DiscoveryContext) -> bool:
        with self.store.transaction(write=True) as transaction:
            value = transaction.get("discovery.manual", game_id)
            if value is None:
                return False
            raw = value.get("paths")
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ModMeshError(ErrorCode.STORAGE, "Invalid saved game path configuration")
            key = _path_key(path, context.platform)
            remaining = [item for item in raw if _path_key(Path(item), context.platform) != key]
            transaction.put("discovery.manual", game_id, {"paths": remaining})
            return len(remaining) != len(raw)

    def discover(
        self,
        context: DiscoveryContext,
        *,
        game_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
        candidate_limit: int = 256,
    ) -> DiscoveryResult:
        if not 1 <= candidate_limit <= 10_000:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid discovery candidate limit")
        adapters = (self.registry.get(game_id),) if game_id else self.registry.all()
        records: dict[tuple[str, str], DiscoveryRecord] = {}
        issues: list[EnvironmentIssue] = list(self.registry.issues)
        for adapter in adapters:
            key = adapter.game.game_id
            if context.platform not in adapter.game.platforms:
                issues.append(
                    EnvironmentIssue("unsupported_platform", f"{key}: unsupported game platform")
                )
                continue
            seen: set[str] = set()
            try:
                manual = self.manual_paths(key)
                manual_keys = {_path_key(path, context.platform) for path in manual}

                def automatic_candidates(current: GameAdapter = adapter) -> Iterator[Path]:
                    yield from current.candidates(context)

                for count, candidate in enumerate(chain(manual, automatic_candidates())):
                    if cancelled is not None and cancelled():
                        raise ModMeshError(ErrorCode.CANCELLED, "Game discovery cancelled")
                    if count >= candidate_limit:
                        issues.append(
                            EnvironmentIssue(
                                "candidate_limit", f"{key}: candidate scan limit reached"
                            )
                        )
                        break
                    path_key = _path_key(candidate, context.platform)
                    if path_key in seen:
                        continue
                    seen.add(path_key)
                    try:
                        installation = adapter.inspect(candidate.resolve(), context.platform)
                        if installation is None:
                            if path_key in manual_keys:
                                issues.append(
                                    EnvironmentIssue(
                                        "manual_path_invalid",
                                        f"{key}: saved path is no longer a recognized game",
                                    )
                                )
                            continue
                        if installation.game_id != key or installation.platform != context.platform:
                            raise ModMeshError(
                                ErrorCode.VALIDATION, "Adapter returned a mismatched game context"
                            )
                        report = adapter.validate(installation)
                        installation_key = _path_key(installation.path, context.platform)
                        existing = records.get((key, installation_key))
                        if existing is not None and existing.origin == "manual":
                            continue
                        stable_id = installation_identity(key, installation.path, context.platform)
                        installation = replace(installation, installation_id=stable_id)
                        record = DiscoveryRecord(
                            installation,
                            report,
                            "manual" if path_key in manual_keys else "automatic",
                        )
                        records[(key, installation_key)] = record
                        payload = asdict(record)
                        payload["installation"]["path"] = str(installation.path)
                        payload["last_seen"] = datetime.now(UTC).isoformat()
                        self.store.put("game.installations", stable_id, payload)
                    except (ModMeshError, OSError) as exc:
                        if isinstance(exc, ModMeshError) and exc.code == ErrorCode.CANCELLED:
                            raise
                        issues.append(
                            EnvironmentIssue("candidate_failed", f"{key}: {redact_text(str(exc))}")
                        )
            except ModMeshError as exc:
                if exc.code == ErrorCode.CANCELLED:
                    raise
                issues.append(EnvironmentIssue("adapter_failed", f"{key}: {exc.message}"))
            except Exception as exc:
                issues.append(EnvironmentIssue("adapter_failed", f"{key}: {redact_text(str(exc))}"))
        return DiscoveryResult(
            tuple(records[key] for key in sorted(records)),
            tuple(issues),
            tuple(adapter.game.game_id for adapter in adapters),
        )
