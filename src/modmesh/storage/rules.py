from collections.abc import Sequence
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import EnvironmentIssue
from modmesh.core.rule_io import parse_rule
from modmesh.core.rule_resources import builtin_registry
from modmesh.core.rules import RuleOrigin, RuleRegistry
from modmesh.storage.installed import string_record
from modmesh.storage.metadata import MetadataStore

NAMESPACE = "rules.installed"


def registry_from_records(
    records: Sequence[tuple[str, dict[str, object]]],
    *,
    issues: list[EnvironmentIssue] | None = None,
) -> RuleRegistry:
    registry = builtin_registry()
    for key, record in records:
        try:
            if set(record) != {"document", "origin", "source", "approval"}:
                raise ModMeshError(ErrorCode.STORAGE, "Invalid saved rule record")
            rule = parse_rule(string_record(record["document"]))
            origin = RuleOrigin(string_record(record["origin"]))
            if rule.game_id != key or origin == RuleOrigin.BUILTIN:
                raise ModMeshError(ErrorCode.STORAGE, "Saved rule identity or origin mismatch")
            approval = record["approval"]
            registry.register(
                rule,
                origin,
                string_record(record["source"]),
                approved_digest=string_record(approval) if approval is not None else None,
            )
        except (ModMeshError, ValueError) as exc:
            if issues is None:
                raise ModMeshError(
                    ErrorCode.STORAGE,
                    "Invalid saved game rule; restore metadata or repair the rule record",
                ) from exc
            issues.append(
                EnvironmentIssue(
                    "invalid_game_rule",
                    "A saved game rule was rejected; original data was retained",
                )
            )
    return registry


def load_rules(database: Path, *, issues: list[EnvironmentIssue] | None = None) -> RuleRegistry:
    if not database.exists():
        return builtin_registry()
    with MetadataStore(database) as store:
        return registry_from_records(store.list(NAMESPACE), issues=issues)
