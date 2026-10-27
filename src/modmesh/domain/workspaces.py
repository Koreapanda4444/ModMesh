from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.models import GameInstallation, Platform
from modmesh.domain.validation import require_id, require_text


def installation_identity(game_id: str, path: Path, platform: Platform) -> str:
    require_id(game_id, "game ID")
    value = str(path.resolve())
    key = value.casefold() if platform == Platform.WINDOWS else value
    return str(uuid5(NAMESPACE_URL, f"modmesh:installation:{game_id}:{key}"))


@dataclass(frozen=True)
class PackIdentity:
    pack_id: str
    game_id: str

    def __post_init__(self) -> None:
        require_id(self.game_id, "game ID")
        try:
            if str(UUID(self.pack_id)) != self.pack_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ModMeshError(ErrorCode.VALIDATION, "Pack ID must be a canonical UUID") from exc

    @classmethod
    def create(cls, game_id: str) -> PackIdentity:
        return cls(str(uuid4()), game_id)


@dataclass(frozen=True)
class ActivationTarget:
    installation_id: str
    profile_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.installation_id, "installation ID")
        if self.profile_id is not None:
            require_text(self.profile_id, "local profile ID")

    @property
    def target_id(self) -> str:
        return str(
            uuid5(NAMESPACE_URL, f"modmesh:target:{self.installation_id}:{self.profile_id!r}")
        )


@dataclass(frozen=True)
class RuntimeContext:
    game_id: str
    installation_id: str
    edition: str
    platform: Platform
    game_version: str | None
    build_id: str | None
    loader: str | None
    loader_version: str | None

    @classmethod
    def capture(cls, installation: GameInstallation) -> RuntimeContext:
        return cls(
            installation.game_id,
            installation.installation_id,
            installation.edition,
            installation.platform,
            installation.version.value,
            installation.version.build_id,
            installation.loader.name if installation.loader else None,
            installation.loader.version if installation.loader else None,
        )
