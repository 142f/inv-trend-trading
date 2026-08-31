"""Deterministic and safe hyper-parameter grid expansion."""

from __future__ import annotations

import ast
from dataclasses import fields
from itertools import product
from typing import Any, Mapping

from inv_trend.adapters.multi_asset.models.domain import AssetSpec, TurtleRules

from .models import BacktestPlan, canonical_hash


_RULE_FIELDS = {field.name for field in fields(TurtleRules)}
_ASSET_FIELDS = {
    field.name for field in fields(AssetSpec)
    if field.name not in {"symbol", "asset_class", "cluster", "cost_bps", "slippage_bps"}
}
_EXECUTION_FIELDS = {"initial_equity", "cash_model", "liquidate_at_end"}
_SIGNAL_RULE_FIELDS = {
    "n_period", "fast_entry", "slow_entry", "fast_exit", "slow_exit",
    "breakout_buffer_n", "trigger_mode", "entry_ma_period",
    "fast_system_enabled", "slow_system_enabled", "skip_fast_after_win", "allow_short",
}


def normalize_parameter_name(name: str, symbols: tuple[str, ...]) -> str:
    if name.startswith("turtle.rules."):
        name = "rules." + name.removeprefix("turtle.rules.")
    elif name.startswith("risk.") and name.removeprefix("risk.") in _EXECUTION_FIELDS:
        name = "execution." + name.removeprefix("risk.")
    if name.startswith("rules.") and name.removeprefix("rules.") in _RULE_FIELDS:
        return name
    if name.startswith("execution.") and name.removeprefix("execution.") in _EXECUTION_FIELDS:
        return name
    parts = name.split(".")
    if len(parts) == 3 and parts[0] == "assets":
        if parts[1].upper() not in symbols:
            raise ValueError(f"parameter references a symbol outside the plan: {name}")
        if parts[2] in _ASSET_FIELDS:
            return f"assets.{parts[1].upper()}.{parts[2]}"
    raise ValueError(f"unsupported or non-searchable parameter: {name}")


def expand_parameter_grid(plan: BacktestPlan) -> tuple[dict[str, Any], ...]:
    normalized: dict[str, tuple[Any, ...]] = {}
    for raw_name, values in plan.parameter_space.items():
        name = normalize_parameter_name(raw_name, plan.symbols)
        if name in normalized:
            raise ValueError(f"duplicate normalized parameter: {name}")
        normalized[name] = tuple(values)
    if not normalized:
        return ({},)
    names = tuple(sorted(normalized))
    raw_count = 1
    for name in names:
        raw_count *= len(normalized[name])
    if raw_count > plan.max_combinations:
        raise ValueError(
            f"parameter grid expands to {raw_count} combinations; "
            f"max_combinations is {plan.max_combinations}"
        )
    combinations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for values in product(*(normalized[name] for name in names)):
        candidate = dict(zip(names, values, strict=True))
        if not _rules_valid(candidate):
            continue
        if not all(_constraint_matches(item, candidate) for item in plan.constraints):
            continue
        digest = canonical_hash(candidate)
        if digest not in seen:
            seen.add(digest)
            combinations.append(candidate)
    if not combinations:
        raise ValueError("no valid parameter combinations remain after constraints")
    return tuple(combinations)


def combination_id(parameters: Mapping[str, Any]) -> str:
    return "组合-" + canonical_hash(parameters)[:12]


def signal_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in parameters.items()
        if (
            key.startswith("rules.")
            and key.removeprefix("rules.") in _SIGNAL_RULE_FIELDS
        )
    }


def _rules_valid(candidate: Mapping[str, Any]) -> bool:
    values = {key.removeprefix("rules."): value for key, value in candidate.items() if key.startswith("rules.")}
    defaults = TurtleRules()
    fast_entry = int(values.get("fast_entry", defaults.fast_entry))
    slow_entry = int(values.get("slow_entry", defaults.slow_entry))
    fast_exit = int(values.get("fast_exit", defaults.fast_exit))
    slow_exit = int(values.get("slow_exit", defaults.slow_exit))
    if not (1 < fast_exit < fast_entry and 1 < slow_exit < slow_entry):
        return False
    try:
        TurtleRules(**values)
    except (TypeError, ValueError):
        return False
    return True


def _constraint_matches(expression: str, values: Mapping[str, Any]) -> bool:
    """Evaluate a deliberately small comparison-only expression language."""

    aliases = {key.replace(".", "__"): value for key, value in values.items()}
    rewritten = expression
    for key in sorted(values, key=len, reverse=True):
        rewritten = rewritten.replace(key, key.replace(".", "__"))
    tree = ast.parse(rewritten, mode="eval")
    allowed = (
        ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
        ast.Compare, ast.Name, ast.Load, ast.Constant, ast.Eq, ast.NotEq,
        ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Tuple, ast.List,
    )
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise ValueError(f"unsupported constraint expression: {expression}")
    unknown = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - set(aliases)
    if unknown:
        raise ValueError(f"constraint references unknown parameters: {sorted(unknown)}")
    return bool(eval(compile(tree, "<backtest-constraint>", "eval"), {"__builtins__": {}}, aliases))


__all__ = ["combination_id", "expand_parameter_grid", "signal_parameters"]
