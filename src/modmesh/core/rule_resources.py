from functools import lru_cache
from importlib.resources import files

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import GameRule
from modmesh.core.rule_io import parse_rule
from modmesh.core.rules import RuleOrigin, RuleRegistry


@lru_cache(maxsize=1)
def builtin_rules() -> tuple[GameRule, ...]:
    directory = files("modmesh").joinpath("rules")
    return tuple(
        parse_rule(directory.joinpath(name + ".json").read_text(encoding="utf-8"))
        for name in ("ark-evolved", "minecraft", "palworld")
    )


def builtin_rule(game_id: str) -> GameRule:
    for rule in builtin_rules():
        if rule.game_id == game_id:
            return rule
    raise ModMeshError(ErrorCode.NOT_FOUND, "Built-in game rule is unavailable")


def builtin_registry() -> RuleRegistry:
    registry = RuleRegistry()
    for rule in builtin_rules():
        registry.register(rule, RuleOrigin.BUILTIN, "modmesh/rules/" + rule.game_id + ".json")
    return registry
