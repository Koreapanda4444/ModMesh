from dataclasses import dataclass
from enum import StrEnum

from modmesh.domain.validation import require_text


class GameCapability(StrEnum):
    DISCOVERY = "discovery"
    VERSION = "version"
    MOD_DISCOVERY = "mod_discovery"
    INSTALL_RULES = "install_rules"
    REMOVAL_RULES = "removal_rules"
    PROFILES = "profiles"
    WORKSHOP_HANDOFF = "workshop_handoff"
    LAUNCH = "launch"
    SYNC = "sync"


class SupportState(StrEnum):
    AVAILABLE = "available"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CapabilityEvidence:
    scope: str
    description: str
    verified: bool = False

    def __post_init__(self) -> None:
        require_text(self.scope, "evidence scope")
        require_text(self.description, "evidence description")


@dataclass(frozen=True)
class CapabilitySupport:
    capability: GameCapability
    state: SupportState
    message: str
    prerequisites: tuple[str, ...] = ()
    evidence: tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.message, "capability message")
        for prerequisite in self.prerequisites:
            require_text(prerequisite, "capability prerequisite")

    @property
    def actionable(self) -> bool:
        return self.state in {SupportState.AVAILABLE, SupportState.HANDOFF}
