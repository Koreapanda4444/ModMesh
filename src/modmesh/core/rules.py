from dataclasses import dataclass
from enum import StrEnum

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import GameRule, LoaderKind
from modmesh.core.rule_io import parse_rule
from modmesh.domain.validation import require_text


class RuleOrigin(StrEnum):
    BUILTIN = "builtin"
    USER = "user"
    COMMUNITY = "community"


@dataclass(frozen=True)
class RegisteredRule:
    rule: GameRule
    origin: RuleOrigin
    source: str
    approved: bool

    @property
    def trust(self) -> str:
        if self.origin == RuleOrigin.BUILTIN:
            return "bundled"
        return "user-approved" if self.approved else "unapproved"


class RuleRegistry:
    def __init__(self, protected: frozenset[str] | None = None) -> None:
        self.protected = (
            frozenset({"minecraft", "palworld", "ark-evolved"}) if protected is None else protected
        )
        self._entries: dict[str, RegisteredRule] = {}

    def register(
        self,
        rule: GameRule,
        origin: RuleOrigin,
        source: str,
        *,
        approved_digest: str | None = None,
        expected_digest: str | None = None,
    ) -> RegisteredRule:
        rule = parse_rule(rule.to_json())
        require_text(source, "rule source")
        if len(source) > 256 or any(not char.isprintable() for char in source):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid rule source label")
        if origin != RuleOrigin.BUILTIN and (
            rule.game_id in self.protected or rule.loader.strategy == LoaderKind.LEGACY_MINECRAFT
        ):
            raise ModMeshError(
                ErrorCode.CONFLICT, "User rules cannot replace built-in game behavior"
            )
        if approved_digest is not None and approved_digest != rule.digest:
            raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact reviewed rule digest")
        approved = origin == RuleOrigin.BUILTIN or approved_digest == rule.digest
        previous = self._entries.get(rule.game_id)
        if previous is not None:
            if previous.origin == origin and previous.rule.digest == rule.digest:
                approved = previous.approved or approved
            else:
                priorities = {RuleOrigin.BUILTIN: 3, RuleOrigin.USER: 2, RuleOrigin.COMMUNITY: 1}
                if (
                    previous.origin == RuleOrigin.BUILTIN
                    or priorities[origin] < priorities[previous.origin]
                ):
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "A higher-trust game rule already exists"
                    )
                if (
                    not approved
                    or expected_digest != previous.rule.digest
                    or rule.revision <= previous.rule.revision
                ):
                    raise ModMeshError(
                        ErrorCode.CONFLICT,
                        "Rule update needs prior digest approval and a newer revision",
                    )
        elif expected_digest is not None:
            raise ModMeshError(ErrorCode.CONFLICT, "The reviewed previous rule no longer exists")
        elif len(self._entries) >= 256:
            raise ModMeshError(ErrorCode.VALIDATION, "Game rule registry limit reached")
        entry = RegisteredRule(rule, origin, source, approved)
        self._entries[rule.game_id] = entry
        return entry

    def get(self, game_id: str, *, include_pending: bool = False) -> RegisteredRule:
        result = self._entries.get(game_id)
        if result is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Game rule is not registered")
        if not result.approved and not include_pending:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Review and approve this game rule first")
        return result

    def all(self, *, include_pending: bool = False) -> tuple[RegisteredRule, ...]:
        return tuple(
            entry for _, entry in sorted(self._entries.items()) if entry.approved or include_pending
        )
