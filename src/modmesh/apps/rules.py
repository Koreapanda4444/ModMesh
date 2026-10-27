from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from modmesh.adapters.games.rule_based import RuleGameAdapter
from modmesh.core.capabilities import CapabilitySupport
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.discovery import manual_path_record
from modmesh.core.game_rules import GameRule
from modmesh.core.games import DiscoveryContext
from modmesh.core.rule_io import parse_rule
from modmesh.core.rules import RegisteredRule, RuleOrigin
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore
from modmesh.storage.rules import NAMESPACE, load_rules, registry_from_records


@dataclass(frozen=True)
class RuleReview:
    rule: GameRule
    previous_digest: str | None
    installation: GameInstallation | None
    support: tuple[CapabilitySupport, ...]
    dry_run: bool = True


@dataclass(frozen=True)
class RuleRegistration:
    entry: RegisteredRule
    installation: GameInstallation | None
    backup: Path | None


class RuleRegistrationService:
    def __init__(self, database: Path, context: DiscoveryContext) -> None:
        self.database, self.context = database, context

    def review(self, text: str, *, path: Path | None = None, source: str = "editor") -> RuleReview:
        rule = parse_rule(text)
        registry = load_rules(self.database)
        previous = next(
            (
                entry.rule.digest
                for entry in registry.all(include_pending=True)
                if entry.rule.game_id == rule.game_id
            ),
            None,
        )
        entry = registry.register(
            rule, RuleOrigin.USER, source, approved_digest=rule.digest, expected_digest=previous
        )
        adapter = RuleGameAdapter(entry)
        installation = None
        if path is not None:
            installation = adapter.inspect(path.expanduser().resolve(), self.context.platform)
            if installation is None or not adapter.validate(installation).valid:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Game path does not satisfy the reviewed rule"
                )
        return RuleReview(rule, previous, installation, adapter.support(installation))

    def register(
        self,
        text: str,
        approval: str,
        *,
        expected_digest: str | None = None,
        path: Path | None = None,
        source: str = "editor",
        cancelled: Callable[[], bool] | None = None,
    ) -> RuleRegistration:
        existed = self.database.exists()
        review = self.review(text, path=path, source=source)
        if approval != review.rule.digest or expected_digest != review.previous_digest:
            raise ModMeshError(
                ErrorCode.CONFLICT, "Rule changed; approve its content and previous digest again"
            )
        if cancelled is not None and cancelled():
            raise ModMeshError(ErrorCode.CANCELLED, "Rule registration cancelled")
        backup = None
        with MetadataStore(self.database) as store:
            if existed:
                backup = store.backup(self.database.parent / "backups" / f"rules-{uuid4()}.sqlite3")
            with store.transaction(write=True) as transaction:
                registry = registry_from_records(transaction.list(NAMESPACE))
                entry = registry.register(
                    review.rule,
                    RuleOrigin.USER,
                    source,
                    approved_digest=approval,
                    expected_digest=expected_digest,
                )
                transaction.put(
                    NAMESPACE,
                    entry.rule.game_id,
                    {
                        "document": entry.rule.to_json(),
                        "origin": entry.origin.value,
                        "source": source,
                        "approval": approval,
                    },
                )
                if review.installation is not None:
                    transaction.put(
                        "discovery.manual",
                        entry.rule.game_id,
                        manual_path_record(
                            transaction.get("discovery.manual", entry.rule.game_id),
                            review.installation.path,
                            self.context.platform,
                        ),
                    )
        return RuleRegistration(entry, review.installation, backup)
