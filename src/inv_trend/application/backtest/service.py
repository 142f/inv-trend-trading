"""Unified batch backtest orchestration, ranking, and diagnostics."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import math
from typing import Any, Mapping
from uuid import uuid4
import warnings

import numpy as np
import pandas as pd

from .execution import UnifiedBacktestExecutor
from .grid import combination_id, expand_parameter_grid
from .metrics import (
    curve_rows, drawdown_rows, performance_metrics, side_metrics, trade_breakdown,
)
from .models import (
    BacktestBatchResult, BacktestCombinationResult, BacktestPlan,
    BacktestSourceBundle, FoldResult, FoldWindow, canonical_hash,
)
from .projector import StrategyReplayProjector, execution_settings, resolve_specs
from .validation import build_walk_forward_windows


class BacktestBatchService:
    """Run deterministic single/grid batches through injected strategy ports."""

    def __init__(
        self,
        *,
        signal_adapter_factory: Any = StrategyReplayProjector,
        executor_factory: Any = UnifiedBacktestExecutor,
    ) -> None:
        self.signal_adapter_factory = signal_adapter_factory
        self.executor_factory = executor_factory

    def run(
        self,
        source: BacktestSourceBundle,
        plan: BacktestPlan,
        data: Mapping[str, pd.DataFrame],
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> BacktestBatchResult:
        _validate_comparability(source, plan, data)
        combinations = expand_parameter_grid(plan)
        calendar = _common_calendar(data)
        windows, holdout, validation_status = build_walk_forward_windows(
            calendar, plan.validation
        )
        projector = self.signal_adapter_factory(source)
        results: list[BacktestCombinationResult] = []
        for parameters in combinations:
            try:
                results.append(
                    self._run_combination(
                        source, plan, data, parameters, projector,
                        windows=windows, holdout=holdout,
                    )
                )
            except Exception as exc:  # isolate one invalid/runtime combination
                results.append(_failed_combination(parameters, exc))
        ranked, best_id, stable_ids = _rank_results(
            results, plan, validation_status, holdout is not None
        )
        generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        assumptions = {
            "source_hash": source.source_hash,
            "symbols": plan.symbols,
            "data_versions": {item.symbol: item.dataset_version for item in source.instruments},
            "initial_equity": plan.initial_equity,
            "cash_model": plan.cash_model,
            "liquidate_at_end": plan.liquidate_at_end,
            "validation": plan.validation,
            "ranking": plan.ranking,
            "fill_model": "signal close; next available bar open",
            "cost_model": "fixed AssetSpec cost_bps + slippage_bps",
            "slippage_model": "cash_cost_equivalent",
            "window_boundary_policy": plan.window_boundary_policy,
        }
        conclusion = _batch_conclusion(ranked, validation_status, best_id, stable_ids)
        return BacktestBatchResult(
            run_id=run_id or uuid4().hex,
            report_date=generated.date().isoformat(),
            source=source,
            plan=plan,
            signal_bundles=projector.bundles,
            combinations=tuple(ranked),
            validation_status=validation_status,
            best_combination_id=best_id,
            stable_combination_ids=stable_ids,
            comparison_assumptions_hash=canonical_hash(assumptions),
            generated_at=generated.isoformat(),
            conclusion=conclusion,
        )

    def _run_combination(
        self,
        source: BacktestSourceBundle,
        plan: BacktestPlan,
        data: Mapping[str, pd.DataFrame],
        parameters: Mapping[str, Any],
        projector: StrategyReplayProjector,
        *,
        windows: tuple[FoldWindow, ...],
        holdout: tuple[str, str] | None,
    ) -> BacktestCombinationResult:
        bundle, store, rules = projector.project(data, parameters)
        specs = resolve_specs(source, parameters)
        settings = execution_settings(parameters, {
            "initial_equity": plan.initial_equity,
            "cash_model": plan.cash_model,
            "liquidate_at_end": plan.liquidate_at_end,
        })

        def execute(start: str | None = None, end: str | None = None):
            strategy = projector.strategy(bundle, specs, rules)
            return self.executor_factory(
                data, specs, rules,
                initial_equity=float(settings["initial_equity"]),
                cash_model=str(settings["cash_model"]),
                liquidate_at_end=bool(settings["liquidate_at_end"]) if end is None else False,
                evaluation_start=start,
                evaluation_end=end,
                market_data=store,
                strategy=strategy,
            ).run()

        full = execute()
        metrics, unavailable = _result_metrics(full)
        fold_results: list[FoldResult] = []
        for window in windows:
            train = execute(window.train_start, window.train_end)
            validation = execute(window.validation_start, window.validation_end)
            train_metrics, _ = _result_metrics(train)
            validation_metrics, _ = _result_metrics(validation)
            fold_results.append(FoldResult(window, train_metrics, validation_metrics))
        validation_metrics = _aggregate_fold_metrics(fold_results, "validation_metrics")
        holdout_metrics: Mapping[str, Any] = {}
        if holdout is not None:
            holdout_result = execute(*holdout)
            holdout_metrics, _ = _result_metrics(holdout_result)
        trades = _records(full.trades)
        orders = _records(full.orders)
        return BacktestCombinationResult(
            combination_id=combination_id(parameters),
            parameters=dict(parameters),
            signal_parameter_hash=bundle.signal_parameter_hash,
            status="COMPLETED",
            metrics=metrics,
            validation_metrics=validation_metrics,
            holdout_metrics=holdout_metrics,
            folds=tuple(fold_results),
            long_metrics=side_metrics(full.trades, "long"),
            short_metrics=side_metrics(full.trades, "short"),
            breakdowns=trade_breakdown(
                full.trades, ("side_name", "symbol", "system", "exit_type")
            ),
            equity_curve=curve_rows(full.equity_curve),
            drawdown_curve=drawdown_rows(full.equity_curve),
            trades=trades,
            orders=orders,
            unavailable=unavailable,
        )


def _result_metrics(result: Any) -> tuple[dict[str, Any], dict[str, str]]:
    return performance_metrics(
        result.equity_curve,
        result.trades,
        result.orders,
        open_position_count=int(getattr(result, "open_position_count", 0)),
        unrealized_pnl=float(getattr(result, "unrealized_pnl", 0.0)),
        pending_intent_count=int(getattr(result, "pending_intent_count", 0)),
    )


def _aggregate_fold_metrics(
    folds: list[FoldResult], attribute: str
) -> dict[str, Any]:
    if not folds:
        return {}
    rows = [dict(getattr(fold, attribute)) for fold in folds]
    returns = [_number(row.get("total_return")) for row in rows]
    valid_returns = [value for value in returns if value is not None]
    aggregate: dict[str, Any] = {
        "fold_count": len(folds),
        "bankrupt": any(bool(row.get("bankrupt")) for row in rows),
        "trade_count": int(sum(int(row.get("trade_count") or 0) for row in rows)),
        "closed_trade_count": int(
            sum(int(row.get("closed_trade_count") or 0) for row in rows)
        ),
        "open_position_count": int(
            sum(int(row.get("open_position_count") or 0) for row in rows)
        ),
        "unrealized_pnl": float(
            sum(float(row.get("unrealized_pnl") or 0.0) for row in rows)
        ),
        "pending_intent_count": int(
            sum(int(row.get("pending_intent_count") or 0) for row in rows)
        ),
        "total_return": (
            float(np.prod([1 + value for value in valid_returns]) - 1)
            if valid_returns else None
        ),
        "positive_fold_ratio": (
            float(sum(value > 0 for value in valid_returns) / len(valid_returns))
            if valid_returns else None
        ),
        "max_drawdown": min(
            (_number(row.get("max_drawdown")) for row in rows),
            key=lambda value: float("inf") if value is None else value,
        ),
    }
    for name in (
        "annualized_return", "sharpe_ratio", "sortino_ratio", "mar_ratio",
        "volatility", "win_rate",
    ):
        values = [_number(row.get(name)) for row in rows]
        finite = [value for value in values if value is not None]
        aggregate[name] = float(np.mean(finite)) if finite else None
    cagrs = [_number(row.get("annualized_return")) for row in rows]
    finite_cagrs = np.asarray([item for item in cagrs if item is not None], dtype=float)
    if len(finite_cagrs):
        median = float(np.median(finite_cagrs))
        q1, q3 = np.percentile(finite_cagrs, [25, 75])
        dispersion = float((q3 - q1) / (abs(median) + 1e-12))
        dispersion_score = 100.0 / (1.0 + max(dispersion, 0.0))
        positive = float(aggregate.get("positive_fold_ratio") or 0.0) * 100.0
        aggregate["annualized_return_iqr_ratio"] = dispersion
        aggregate["stability_score"] = 0.5 * positive + 0.5 * dispersion_score
    return aggregate


def _rank_results(
    results: list[BacktestCombinationResult],
    plan: BacktestPlan,
    validation_status: str,
    has_holdout: bool,
) -> tuple[list[BacktestCombinationResult], str | None, tuple[str, ...]]:
    if validation_status != "READY":
        return results, None, ()
    qualified_ids: set[str] = set()
    for result in results:
        values = result.validation_metrics
        drawdown = _number(values.get("max_drawdown"))
        trades = int(values.get("closed_trade_count") or 0)
        if (
            result.status == "COMPLETED"
            and not bool(values.get("bankrupt"))
            and trades >= plan.ranking.min_oos_trades
            and drawdown is not None
            and abs(drawdown) <= plan.ranking.max_drawdown
        ):
            qualified_ids.add(result.combination_id)
    qualified = [item for item in results if item.combination_id in qualified_ids]
    percentiles = {
        name: _percentiles(qualified, name)
        for name in ("sharpe_ratio", "mar_ratio", "annualized_return")
    }
    scored: list[BacktestCombinationResult] = []
    for result in results:
        if result.combination_id not in qualified_ids:
            scored.append(replace(result, qualified=False))
            continue
        stability = float(result.validation_metrics.get("stability_score") or 0.0)
        score = (
            plan.ranking.sharpe_weight
            * percentiles["sharpe_ratio"][result.combination_id]
            + plan.ranking.mar_weight * percentiles["mar_ratio"][result.combination_id]
            + plan.ranking.cagr_weight
            * percentiles["annualized_return"][result.combination_id]
            + plan.ranking.stability_weight * stability
        )
        scored.append(replace(result, qualified=True, score=round(score, 6), stability_score=stability))
    ordered = sorted(
        scored,
        key=lambda item: (
            item.score is not None,
            -math.inf if item.score is None else item.score,
            item.combination_id,
        ),
        reverse=True,
    )
    holdout_order = sorted(
        [item for item in ordered if item.qualified],
        key=lambda item: (
            _sort_number(item.holdout_metrics.get("sharpe_ratio")),
            _sort_number(item.holdout_metrics.get("annualized_return")),
        ), reverse=True,
    )
    holdout_rank = {item.combination_id: pos + 1 for pos, item in enumerate(holdout_order)}
    ranked: list[BacktestCombinationResult] = []
    formal_rank = 0
    for item in ordered:
        rank = None
        if item.qualified:
            formal_rank += 1
            rank = formal_rank
        risk = _overfit_risk(item, rank, holdout_rank, len(holdout_order))
        ranked.append(replace(item, rank=rank, overfit_risk=risk))
    ranked = _add_neighbor_risk(ranked, plan)
    candidate = next((item for item in ranked if item.rank == 1), None)
    # Freeze the neighborhood using development validation alone. The final
    # holdout may reject that candidate, never pick a replacement or a neighbor.
    stable = _stable_region(ranked, plan, candidate)
    holdout_return = _number(candidate.holdout_metrics.get("total_return")) if candidate else None
    passed_holdout = bool(
        candidate is not None
        and has_holdout
        and holdout_return is not None and holdout_return >= 0
    )
    best_id = candidate.combination_id if passed_holdout else None
    return ranked, best_id, stable if passed_holdout else ()


def _add_neighbor_risk(
    ranked: list[BacktestCombinationResult], plan: BacktestPlan
) -> list[BacktestCombinationResult]:
    updated: list[BacktestCombinationResult] = []
    for item in ranked:
        risk = dict(item.overfit_risk)
        flags = list(risk.get("flags", ()))
        neighbor_scores = [
            float(other.score) for other in ranked
            if other.score is not None
            and other.combination_id != item.combination_id
            and _grid_neighbors(item.parameters, other.parameters, plan)
        ]
        if item.score is not None and neighbor_scores:
            median = float(np.median(neighbor_scores))
            ratio = median / item.score if item.score > 0 else 1.0
            risk["neighbor_median_score"] = median
            if ratio < 0.70:
                flags.append({"code": "PARAMETER_CLIFF", "value": ratio})
        risk["flags"] = flags
        risk["level"] = "HIGH" if len(flags) >= 3 else "MEDIUM" if flags else "LOW"
        updated.append(replace(item, overfit_risk=risk))
    return updated


def _percentiles(
    results: list[BacktestCombinationResult], metric: str
) -> dict[str, float]:
    pairs = [
        (item.combination_id, _number(item.validation_metrics.get(metric)))
        for item in results
    ]
    finite = sorted((value, name) for name, value in pairs if value is not None)
    if not finite:
        return {name: 0.0 for name, _ in pairs}
    scores = {name: 0.0 for name, _ in pairs}
    if len(finite) == 1:
        scores[finite[0][1]] = 50.0
        return scores
    # Equal statistics get equal scores; IDs must not break financial ties.
    ranks = pd.Series([value for value, _ in finite]).rank(method="average")
    scores.update({name: 100.0 * (float(rank) - 1) / (len(finite) - 1)
                   for (_, name), rank in zip(finite, ranks)})
    return scores


def _overfit_risk(
    item: BacktestCombinationResult,
    validation_rank: int | None,
    holdout_rank: Mapping[str, int],
    qualified_count: int,
) -> dict[str, Any]:
    flags: list[dict[str, Any]] = []
    train_sharpe_values = [
        _number(fold.train_metrics.get("sharpe_ratio")) for fold in item.folds
    ]
    train_finite = [value for value in train_sharpe_values if value is not None]
    train_sharpe = float(np.mean(train_finite)) if train_finite else None
    oos_sharpe = _number(item.validation_metrics.get("sharpe_ratio"))
    if train_sharpe is not None and train_sharpe > 0 and oos_sharpe is not None:
        degradation = (train_sharpe - oos_sharpe) / abs(train_sharpe)
        if degradation > 0.5:
            flags.append({"code": "IS_OOS_SHARPE_DECAY", "value": degradation})
    if validation_rank is not None and qualified_count:
        test_rank = holdout_rank.get(item.combination_id, qualified_count)
        if test_rank > max(1, math.ceil(qualified_count / 4)):
            flags.append({"code": "HOLDOUT_RANK_DROP", "value": test_rank})
    for field, code in (("side_name", "DIRECTION_CONCENTRATION"), ("symbol", "SYMBOL_CONCENTRATION")):
        breakdown = item.breakdowns.get(field, {})
        total_abs = sum(abs(float(row.get("pnl") or 0.0)) for row in breakdown.values())
        if total_abs:
            concentration = max(
                abs(float(row.get("pnl") or 0.0)) for row in breakdown.values()
            ) / total_abs
            if concentration > 0.70:
                flags.append({"code": code, "value": concentration})
    if int(item.validation_metrics.get("trade_count") or 0) < 30:
        flags.append({"code": "SMALL_TRADE_SAMPLE", "value": item.validation_metrics.get("trade_count", 0)})
    level = "HIGH" if len(flags) >= 3 else "MEDIUM" if flags else "LOW"
    return {"level": level, "flags": flags, "train_sharpe": train_sharpe, "oos_sharpe": oos_sharpe}


def _stable_region(
    ranked: list[BacktestCombinationResult],
    plan: BacktestPlan,
    candidate: BacktestCombinationResult | None,
) -> tuple[str, ...]:
    if candidate is None or candidate.score is None:
        return ()
    eligible = {
        item.combination_id: item for item in ranked
        if item.qualified
        and item.score is not None
        and item.score >= candidate.score * 0.90
        and float(item.validation_metrics.get("positive_fold_ratio") or 0.0) >= 2 / 3
    }
    visited: set[str] = set()
    pending = [candidate.combination_id]
    while pending:
        current_id = pending.pop()
        if current_id in visited or current_id not in eligible:
            continue
        visited.add(current_id)
        current = eligible[current_id]
        pending.extend(
            other_id for other_id, other in eligible.items()
            if other_id not in visited and _grid_neighbors(current.parameters, other.parameters, plan)
        )
    return tuple(sorted(visited))


def _grid_neighbors(
    left: Mapping[str, Any], right: Mapping[str, Any], plan: BacktestPlan
) -> bool:
    differences = [key for key in set(left) | set(right) if left.get(key) != right.get(key)]
    if len(differences) != 1:
        return False
    key = differences[0]
    values = None
    for raw_name, candidates in plan.parameter_space.items():
        normalized = raw_name
        if raw_name.startswith("turtle.rules."):
            normalized = "rules." + raw_name.removeprefix("turtle.rules.")
        if normalized == key:
            values = list(candidates)
            break
    if values is None:
        return False
    try:
        return abs(values.index(left[key]) - values.index(right[key])) == 1
    except ValueError:
        return False


def _validate_comparability(
    source: BacktestSourceBundle,
    plan: BacktestPlan,
    data: Mapping[str, pd.DataFrame],
) -> None:
    source_symbols = {item.symbol for item in source.instruments}
    if set(plan.symbols) != source_symbols or set(data) != source_symbols:
        raise ValueError("plan, source bundle, and market data must contain identical symbols")
    for item in source.instruments:
        version = str(data[item.symbol].attrs.get("dataset_version") or "")
        if version != item.dataset_version:
            raise ValueError(f"dataset version mismatch for {item.symbol}")


def _common_calendar(data: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    values: set[pd.Timestamp] = set()
    for frame in data.values():
        values.update(pd.Timestamp(item) for item in frame.index)
    return pd.DatetimeIndex(sorted(values))


def _records(frame: pd.DataFrame) -> tuple[Mapping[str, Any], ...]:
    if frame.empty:
        return ()
    rows: list[Mapping[str, Any]] = []
    for raw in frame.to_dict("records"):
        row: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, pd.Timestamp):
                row[key] = value.isoformat()
            elif isinstance(value, np.generic):
                row[key] = value.item()
            elif isinstance(value, float) and not math.isfinite(value):
                row[key] = None
            else:
                row[key] = value
        rows.append(row)
    return tuple(rows)


def _failed_combination(
    parameters: Mapping[str, Any], exc: Exception
) -> BacktestCombinationResult:
    return BacktestCombinationResult(
        combination_id=combination_id(parameters), parameters=dict(parameters),
        signal_parameter_hash=canonical_hash({}), status="FAILED", metrics={},
        validation_metrics={}, holdout_metrics={}, folds=(), long_metrics={},
        short_metrics={}, breakdowns={}, equity_curve=(), drawdown_curve=(),
        trades=(), orders=(), error=f"{type(exc).__name__}: {exc}",
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sort_number(value: Any) -> float:
    number = _number(value)
    return -math.inf if number is None else number


def _batch_conclusion(
    results: list[BacktestCombinationResult],
    validation_status: str,
    best_id: str | None,
    stable_ids: tuple[str, ...],
) -> Mapping[str, Any]:
    """Build a deterministic research conclusion from already-computed evidence."""

    issues: list[Mapping[str, Any]] = []
    if validation_status != "READY":
        issues.append({
            "code": "INSUFFICIENT_VALIDATION_HISTORY",
            "severity": "HIGH",
            "message": "历史长度不足，不能声明最优参数或稳定区间。",
        })
    failed = [item.combination_id for item in results if item.status != "COMPLETED"]
    if failed:
        issues.append({
            "code": "COMBINATION_FAILURES", "severity": "MEDIUM",
            "message": f"有 {len(failed)} 个参数组合执行失败。", "combinations": failed,
        })
    if best_id is None:
        return {
            "status": "NO_CONFIRMED_CANDIDATE",
            "summary": "没有参数组合同时通过样本外门槛和最终留出确认。",
            "best_combination_id": None,
            "stable_combination_ids": [],
            "key_issues": issues,
        }
    best = next(item for item in results if item.combination_id == best_id)
    risk_level = str(best.overfit_risk.get("level", "UNKNOWN"))
    if len(stable_ids) < 2:
        issues.append({
            "code": "NARROW_STABLE_REGION", "severity": "MEDIUM",
            "message": "稳定区域只有一个网格点，参数邻域鲁棒性证据不足。",
        })
    if risk_level in {"MEDIUM", "HIGH"}:
        issues.append({
            "code": "OVERFIT_RISK", "severity": risk_level,
            "message": f"候选组合的过拟合风险为 {risk_level}。",
            "evidence": best.overfit_risk.get("flags", []),
        })
    return {
        "status": "CANDIDATE_CONFIRMED",
        "summary": "候选组合通过样本外排序和最终留出确认，仅作为研究结论。",
        "best_combination_id": best_id,
        "stable_combination_ids": list(stable_ids),
        "risk_level": risk_level,
        "key_issues": issues,
    }


class StrategyBacktestService(BacktestBatchService):
    """One-release compatibility name for :class:`BacktestBatchService`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "StrategyBacktestService is deprecated; use BacktestBatchService",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


__all__ = ["BacktestBatchService", "StrategyBacktestService"]
