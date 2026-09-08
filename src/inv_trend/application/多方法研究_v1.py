"""Pre-registered causal walk-forward research; full holdout never enters the driver."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
import hashlib
import json
import time
from collections import defaultdict

import numpy as np

from inv_trend.core.阶段协议_v1 import Bar, RawBar, RunContext, digest
from inv_trend.core.在线策略_v1 import Candidate, OnlineFeatures, OnlineStrategy, candidate_grid
from inv_trend.core.事件账户_v1 import CashBroker, ExecutionConfig
from inv_trend.data.时点行情_v1 import PointInTimeRepository, EQUITIES
from inv_trend.storage.阶段运行_v1 import StageRepository
from .标准阶段_v1 import stages, EvaluationStage
from .研究评分_v1 import snapshot_metrics, training_score, equity_metrics, block_bootstrap, review_dimensions

FAMILIES = ("momentum", "breakout", "reversion")


def code_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def replay_training(features, candidate, start, end, execution):
    strategy = OnlineStrategy(candidate)
    broker = CashBroker(execution)
    for f in features[:start]:
        strategy.update(f)
    rows = [broker.on_signal(strategy.update(f), audit=False) for f in features[start:end]]
    return snapshot_metrics(rows)


def train_grid(bars: tuple[Bar, ...], candidates, execution, *, warmup=126,
               folds=3, cache_features=True):
    if len(bars) < warmup + folds * 40:
        raise ValueError("insufficient historical training window")
    size = (len(bars) - warmup) // folds
    cached = {}
    records = []
    ranking = []
    for candidate in candidates:
        key = (candidate.lookback, candidate.exit_lookback)
        if not cache_features or key not in cached:
            engine = OnlineFeatures(candidate)
            features = tuple(engine.update(b, trace=False) for b in bars)
            if cache_features:
                cached[key] = features
        else:
            features = cached[key]
        fs = []
        for fold in range(folds):
            start = warmup + fold * size
            end = warmup + (fold + 1) * size
            metrics = replay_training(features, candidate, start, end, execution)
            fs.append({"scope": "TRAIN", "candidate_id": candidate.id, "fold": fold,
                       "start": bars[start].available_at, "end": bars[end - 1].available_at,
                       "metrics": metrics})
        score, eligible = training_score(fs)
        records.extend({**f, "score": score, "eligible": eligible} for f in fs)
        ranking.append({"candidate_id": candidate.id, "family": candidate.family,
                        "score": score, "eligible": eligible,
                        "folds": [f["metrics"] for f in fs]})
    return ranking, records


def pick(ranking, family, candidates):
    available = [r for r in ranking if r["family"] == family and r["eligible"]]
    if not available:
        return Candidate("cash", 20), {"candidate_id": "cash", "reason": "all training gates failed"}
    chosen = min(available, key=lambda r: (-r["score"], r["candidate_id"]))
    return next(c for c in candidates if c.id == chosen["candidate_id"]), chosen


def calendar_windows():
    return [{"id": f"窗口{(year-2020)*2 + half + 1:02d}",
             "start": f"{year}-{1 if half == 0 else 7:02d}-01T00:00:00+00:00",
             "end": f"{year if half == 0 else year+1}-{7 if half == 0 else 1:02d}-01T00:00:00+00:00"}
            for year in range(2020, 2025) for half in (0, 1)]


class WindowRuntime:
    """One independent Stage chain; advance exactly once for each newly observed bar."""
    def __init__(self, context, candidate, broker, warmup):
        self.context = context
        self.broker = broker
        self.chain = list(stages(context, candidate, broker.config))
        self.chain[4].engine = broker
        self.initial = broker.cash + broker.quantity * (broker.last_bar.close if broker.last_bar else 0)
        self.chain[5] = EvaluationStage(initial_equity=self.initial, initial_fees=broker.fees,
                                        initial_slippage=broker.slippage_cost)
        for b in warmup:
            self.chain[3].engine.update(self.chain[2].engine.update(b))
        self.snapshots = []
        self.signals = []
        self.last_packets = []
        self.initial_fees, self.initial_slip = broker.fees, broker.slippage_cost

    def step(self, raw):
        packets = [self.chain[0].run(raw)]
        for stage in self.chain[1:]:
            packets.append(stage.run(packets[-1]))
        self.snapshots.append(packets[4].payload)
        self.signals.append(packets[3].payload)
        self.last_packets = packets

    def publish(self, repository, study, symbol, family, window, scenario):
        if not self.snapshots:
            raise ValueError("empty OOS window")
        metrics = snapshot_metrics(self.snapshots, initial=self.initial,
                                   initial_fees=self.initial_fees, initial_slippage=self.initial_slip)
        repository.results(study, symbol, family, window["id"], scenario,
                           self.snapshots, self.initial, metrics)
        if scenario == "base":
            repository.decisions(study, symbol, family, window["id"], self.signals)
            repository.publish(self.last_packets,
                               [(self.context.run_id, stage.name, self.last_packets[-1].as_of,
                                 stage.snapshot()) for stage in self.chain])
        return self.snapshots, metrics


def run_study(data_root, study_id, *, symbols=EQUITIES, dataset_version=None,
              window_limit=None, test_evidence=None, cache_features=True):
    """New study ID is mandatory; crash recovery uses new IDs, never rewrites selection locks."""
    repository = StageRepository(data_root)
    if dataset_version is None:
        rows = repository.store.rows("SELECT dataset_hash FROM market_datasets_v3 ORDER BY dataset_id")
        if len(rows) != 1:
            raise ValueError("multiple/no datasets: explicit dataset_version required")
        dataset_version = rows[0]["dataset_hash"]
    if not symbols or any(s not in EQUITIES for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("provide a unique supported equity/ETF universe")
    windows = calendar_windows()
    if window_limit is not None:
        if type(window_limit) is not int or not 1 <= window_limit <= len(windows):
            raise ValueError("window_limit must be 1..10")
        windows = windows[:window_limit]
    candidates = candidate_grid()
    execution = ExecutionConfig()
    version = code_hash()
    protocol = {"schema_version": "1.0", "dataset_version": dataset_version,
                "strategy_version": "在线三方法_v1", "code_version": version,
                "symbols": list(symbols), "windows": windows, "grid": [asdict(c) for c in candidates],
                "warmup_bars": 126, "training_bars": 756, "inner_folds": 3,
                "gap_bars": 5, "holdout_start": "2025-01-01T00:00:00+00:00",
                "selection": "median Sharpe - std Sharpe - 2*worst DD; 2/3 positive; DD<=.35; fills>=6",
                "execution": asdict(execution), "scenarios": ["base", "cost_x2", "delay_2"],
                "universe_basis": "pre-existing uploaded universe; not point-in-time constituent history",
                "source_status": "RESEARCH_ONLY", "production_enabled": False,
                "test_evidence": test_evidence or {}, "retention": "all OOS curves/fills/signals; last-bar seven-stage trace per window",
                "iteration_rule": "engineering fixes only; no OOS-dependent grid/selection edits",
                "development_exposure": "QQQ 2020 inspected in two engineering smoke iterations; not pristine OOS",
                "holdout_scope": "2025+ prices unread by this study; prior project access is not certified",
                "cash_accounts": "independent 100000 per symbol/family; no shared portfolio margin",
                "implementation": "domain training fast path is tested against audited Stage path"}
    repository.freeze(study_id, protocol, candidates)
    started = time.perf_counter()
    all_curves = defaultdict(lambda: defaultdict(dict))
    all_windows = defaultdict(list)
    family_seconds = defaultdict(float)
    with PointInTimeRepository(data_root, dataset_version,
                               allowed_before=protocol["holdout_start"]) as source:
        try:
            for symbol in symbols:
                brokers = {(family, scenario): CashBroker(replace(execution,
                           fee_bps=execution.fee_bps * (2 if scenario == "cost_x2" else 1),
                           slippage_bps=execution.slippage_bps * (2 if scenario == "cost_x2" else 1),
                           delay_bars=2 if scenario == "delay_2" else 1))
                           for family in (*FAMILIES, "buy_hold") for scenario in
                           (protocol["scenarios"] if family in FAMILIES else ("base",))}
                # Window driver reads/finishes ONE window, then advances. It never receives a full test array.
                for win in windows:
                    history = source.history(symbol, end=win["start"], limit=887)
                    if len(history) != 887:
                        raise ValueError(f"{symbol}/{win['id']}: need 887 historical bars, got {len(history)}")
                    train = history[:-5]
                    window = {**win, "cutoff": train[-1].available_at}
                    ts = time.perf_counter()
                    ranking, trial_rows = train_grid(train, candidates, execution, cache_features=cache_features)
                    train_seconds = time.perf_counter() - ts
                    repository.training(study_id, symbol, window["id"], trial_rows)
                    selections = {}
                    for family in FAMILIES:
                        candidate, selection = pick(ranking, family, candidates)
                        lock_hash = repository.lock(study_id, symbol, family, window,
                                                   {**selection, "parameters": asdict(candidate)})
                        selections[family] = (candidate, lock_hash)
                    benchmark = Candidate("buy_hold", 20)
                    benchmark_lock = repository.lock(study_id, symbol, "buy_hold", window,
                                                     {"candidate_id": benchmark.id,
                                                      "parameters": asdict(benchmark),
                                                      "reason": "fixed first-entry buy-and-hold reference"})
                    selections["buy_hold"] = (benchmark, benchmark_lock)
                    runtimes = {}
                    for family, (candidate, lock_hash) in selections.items():
                        for scenario in (protocol["scenarios"] if family in FAMILIES else ("base",)):
                            broker = brokers[(family, scenario)]
                            context = RunContext(f"{study_id}/{symbol}/{family}/{window['id']}/{scenario}",
                                                 dataset_version, "在线三方法_v1", version,
                                                 digest({"parameters": asdict(candidate), "execution": asdict(broker.config), "lock": lock_hash}),
                                                 "OOS" if scenario == "base" else "STRESS")
                            runtimes[(family, scenario)] = WindowRuntime(context, candidate, broker, history[-126:])
                    # All locks have committed. Even the coordinator never materializes a future window.
                    # Every independent scenario advances together from the same newly observed bar.
                    ts = time.perf_counter()
                    observed = 0
                    for raw in source.stream(symbol, start=window["start"], end=window["end"]):
                        for runtime in runtimes.values():
                            runtime.step(raw)
                        observed += 1
                    elapsed = time.perf_counter() - ts + train_seconds
                    for (family, scenario), runtime in runtimes.items():
                        snaps, metrics = runtime.publish(repository, study_id, symbol, family, window, scenario)
                        family_seconds[family] += elapsed / len(runtimes)
                        all_windows[(family, scenario)].append(metrics)
                        for snapshot in snaps:
                            all_curves[(family, scenario)][symbol][snapshot.as_of] = snapshot.equity
                    print(f"{study_id} {symbol} {window['id']} locked -> replayed ({observed} bars)", flush=True)
            report = {"study_id": study_id, "protocol_hash": digest(protocol), "families": {},
                      "distinct_parameters": len(candidates), "training_trials": len(symbols) * len(windows) * len(candidates) * 3,
                      "oos_symbol_windows": len(symbols) * len(windows) * 3,
                      "holdout_accessed": False, "production_enabled": False}
            benchmark_curves = all_curves[("buy_hold", "base")]
            benchmark_dates = sorted(set.intersection(*(set(d) for d in benchmark_curves.values())))
            benchmark_metrics = equity_metrics(
                [sum(benchmark_curves[symbol][day] for symbol in symbols) for day in benchmark_dates],
                initial=100000 * len(symbols),
                fills=sum(m["fill_count"] for m in all_windows[("buy_hold", "base")]),
                fees=sum(m["fees"] for m in all_windows[("buy_hold", "base")]),
                slippage=sum(m["slippage_cost"] for m in all_windows[("buy_hold", "base")]))
            report["buy_hold_reference"] = benchmark_metrics
            for family in FAMILIES:
                scenario_summary = {}
                for scenario in protocol["scenarios"]:
                    curves = all_curves[(family, scenario)]
                    dates = sorted(set.intersection(*(set(d) for d in curves.values())))
                    if any(set(d) != set(dates) for d in curves.values()):
                        raise ValueError("aggregate requires aligned calendars; no silent forward fill")
                    eq = np.asarray([sum(curves[s][d] for s in symbols) for d in dates])
                    scenario_summary[scenario] = equity_metrics(eq, initial=100000 * len(symbols),
                        fills=sum(m["fill_count"] for m in all_windows[(family, scenario)]),
                        fees=sum(m["fees"] for m in all_windows[(family, scenario)]),
                        slippage=sum(m["slippage_cost"] for m in all_windows[(family, scenario)]))
                    if scenario == "base":
                        full = np.r_[100000 * len(symbols), eq]
                        stats = block_bootstrap(np.diff(full) / full[:-1], trials=len(candidates))
                review = review_dimensions(scenario_summary["base"], all_windows[(family, "base")],
                                           [m for x in ("cost_x2", "delay_2") for m in all_windows[(family, x)]],
                                           tests_passed=bool((test_evidence or {}).get("passed")),
                                           seconds=family_seconds[family],
                                           baselines={"cash_return": 0.0, "buy_hold": benchmark_metrics},
                                           code_checks=bool((test_evidence or {}).get("code_checks")))
                review["stress_metrics"] = scenario_summary
                review["bootstrap"] = stats
                repository.review(study_id, family, review)
                report["families"][family] = review
            report["elapsed_seconds"] = time.perf_counter() - started
            report["query_count"] = len(source.query_log)
            report["rows_read"] = source.rows_read
            repository.review(study_id, "研究汇总", report)
            repository.finish(study_id, source.query_log)
            return report
        except BaseException as exc:
            repository.review(study_id, "失败回执", {"error": type(exc).__name__, "message": str(exc),
                                                     "elapsed_seconds": time.perf_counter() - started})
            repository.finish(study_id, source.query_log, failed=True)
            raise
