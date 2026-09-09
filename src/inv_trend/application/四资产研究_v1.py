"""Freeze protocol -> train -> immutable lock -> advance shared OOS account."""
from dataclasses import asdict, replace
import hashlib
from pathlib import Path
import time

import numpy as np

from inv_trend.core.四资产合同_v1 import ASSUMPTION, CORE, DISCLAIMER, research_contract
from inv_trend.core.组合账户_v1 import CostModel, PortfolioAccount
from inv_trend.core.组合风控_v1 import RiskConfig
from inv_trend.data.四资产行情_v1 import load_bundle
from inv_trend.storage.组合研究_v1 import PortfolioRepository
from .组合回测_v1 import Experiment, FeatureCache, experiment_grid, performance, run_backtest

FAMILIES = ("momentum", "breakout", "reversion", "core_tactical")
STRESSES = ("base", "fees_x2", "slippage_x2", "delay_1_extra", "delay_2_extra",
            "execution_worse_20pct", "volatility_shock", "crypto_corr_one",
            "metals_corr_one", "all_corr_spike")


def code_version():
    root = Path(__file__).parents[1]
    selected = sorted([p for p in root.rglob("*.py") if any(x in p.name for x in ("四资产", "组合"))])
    h = hashlib.sha256()
    for p in selected:
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def windows():
    return [dict(id=str(y), train_start=f"{y-2}-01-01T00:00:00+00:00",
                 train_end=f"{y}-01-01T00:00:00+00:00",
                 oos_start=f"{y}-01-01T00:00:00+00:00",
                 oos_end=f"{y+1}-01-01T00:00:00+00:00") for y in range(2021, 2026)]


def training_score(metrics):
    if metrics.get("Sharpe") is None or metrics["MDD"] > .40:
        return -1e6
    return metrics["Sharpe"] - 2 * metrics["MDD"]


def stressed(candidate, scenario):
    costs = candidate.costs
    if scenario == "fees_x2":
        costs = replace(costs, fee_bps=costs.fee_bps * 2)
    elif scenario == "slippage_x2":
        costs = replace(costs, slippage_bps=costs.slippage_bps * 2)
    elif scenario in ("delay_1_extra", "delay_2_extra"):
        costs = replace(costs, delay_bars=costs.delay_bars + (1 if scenario == "delay_1_extra" else 2))
    elif scenario == "execution_worse_20pct":
        costs = replace(costs, slippage_bps=costs.slippage_bps * 1.2)
    return replace(candidate, costs=costs)


def passive(method, target=.12):
    return Experiment("passive", risk=RiskConfig(method=method, portfolio_target_vol=target),
                      costs=CostModel())


def run_research(study_id, *, data_root="data", mode="FORMAL", evidence=None):
    # This is an explicit hard gate, not a warning followed by a silent research fallback.
    bundle = load_bundle(data_root, mode=mode)
    if mode != ASSUMPTION:
        raise ValueError("historical contract verification required")
    repo = PortfolioRepository(data_root)
    version = code_version()
    grid, schedule = experiment_grid(), windows()
    protocol = dict(mode=mode, core=list(CORE), dataset_version=bundle.version,
        code_version=version, strategy_version="four-asset-v1", windows=schedule,
        grid=[asdict(c) for c in grid], design="160 structured coupled profiles; not full factorial",
        selection="train Sharpe - 2*MDD; MDD>40% receives -1e6; ties by parameter ID",
        admission="replayed OOS Sharpe>=0.5, MDD<=0.30, >=3 positive windows, all execution stresses positive; formal gates mandatory",
        scopes={"research_historical": "2019-2020 initial train; later train is expanding observed history",
                "validation_historical": "2021-2025 replayed_oos, not untouched",
                "paper_forward": "NOT_RUN", "pseudo_oos": "not used for acceptance"},
        scenarios=list(STRESSES), risk_match="each TRAIN window calibrates passive target, locked before OOS",
        disclaimer=DISCLAIMER, limitations=["USDT/USD=1", "metal provider label+1 day availability",
            "synthetic funded units, long-only, no real CFD margin", "bid plus assumed execution costs",
            "synthetic capacity, calendar-day fixed carry; not historical funding",
            "covariance shocks are allocator sensitivity, not synthetic market PnL stress",
            "Sharpe assumes zero risk-free rate; uninvested cash earns zero"],
        test_evidence=evidence or {}, robustness="BLOCKED_UNVERIFIED_SPY_TLT_INPUTS")
    repo.begin(study_id, protocol, [research_contract(s) for s in CORE], bundle.lineage)
    start_clock = time.perf_counter()
    cache = FeatureCache(bundle, version)
    accounts, curves, risk_rows, window_metrics, selections = {}, {}, {}, {}, []
    diagnostics = []
    try:
        # Stage 1: fixed single-asset screens, TRAIN only; never changes Core membership.
        first = schedule[0]
        screens = []
        for symbol in CORE:
            for family in FAMILIES[:3]:
                c = Experiment(family, threshold=1.5 if family == "reversion" else 0.)
                # Asset mask preserves strategy timing; single_asset override is reserved for B&H.
                ca = cache.get(c, first["train_start"], first["train_end"])
                old = ca["signals"].copy()
                ca["signals"][:, [i for i, s in enumerate(CORE) if s != symbol]] = 0
                try:
                    _, _, _, m = run_backtest(bundle, c, start=first["train_start"], end=first["train_end"],
                                              code_version=version, cache=cache)
                    screens.append(dict(symbol=symbol, family=family, metrics=m, scope="TRAIN_SCREEN_ONLY"))
                finally:
                    ca["signals"] = old
        repo.record("benchmarks", (study_id, "single_asset_training_screens"), screens)
        for win in schedule:
            trial_rows = []
            for i, c in enumerate(grid):
                try:
                    _, _, _, metrics = run_backtest(bundle, c, start=win["train_start"],
                        end=win["train_end"], code_version=version, cache=cache)
                    trial_rows.append((c, metrics, "COMPLETE", None))
                except (ValueError, ArithmeticError) as exc:
                    trial_rows.append((c, {}, "FAILED", str(exc)))
                if (i + 1) % 40 == 0:
                    print(f"{study_id} TRAIN {win['id']} {i+1}/{len(grid)}", flush=True)
            repo.trials(study_id, win["id"], trial_rows)
            selected, lock_ids = {}, {}
            for family in FAMILIES:
                eligible = [(c, m) for c, m, status, _ in trial_rows if c.family == family and status == "COMPLETE"]
                if not eligible:
                    raise ValueError(f"no evaluable training candidate: {family}")
                candidate, metrics = sorted(eligible, key=lambda x: (-training_score(x[1]), x[0].id))[0]
                selected[family] = candidate
                lock_ids[family] = repo.lock(study_id, win, family, candidate, training_score(metrics), bundle.version, version)
                # Explicit one-axis neighborhood, all on TRAIN; no OOS-guided reselection.
                neighbors = []
                for look in (60, 70, 80, 90, 100):
                    neighbor = replace(candidate, lookback=look)
                    try:
                        _, _, _, nm = run_backtest(bundle, neighbor, start=win["train_start"], end=win["train_end"], code_version=version, cache=cache)
                        neighbors.append(dict(lookback=look, Sharpe=nm["Sharpe"], CAGR=nm["CAGR"], MDD=nm["MDD"]))
                    except (ValueError, ArithmeticError) as exc:
                        neighbors.append(dict(lookback=look, error=str(exc)))
                sharpe_values = [n["Sharpe"] for n in neighbors if n.get("Sharpe") is not None]
                fragile = len(sharpe_values) != 5 or np.ptp(sharpe_values) > 1.0
                diagnostics.append(dict(window=win["id"], family=family, neighbors=neighbors,
                                        fragile=bool(fragile), scope="TRAIN_ONLY"))
                # Passive risk matching is calibrated with this TRAIN only, then locked.
                p = passive("inverse_vol", candidate.risk.portfolio_target_vol)
                _, _, _, pm = run_backtest(bundle, p, start=win["train_start"], end=win["train_end"], code_version=version, cache=cache)
                target = np.clip(p.risk.portfolio_target_vol * metrics["volatility"] / max(pm["volatility"], .01), .01, .50)
                p = replace(p, risk=replace(p.risk, portfolio_target_vol=float(target)))
                selected[family + "_risk_matched"] = p
                lock_ids[family + "_risk_matched"] = repo.lock(study_id, win, family + "_risk_matched", p, None, bundle.version, version)
            # Every selection and risk-matched passive lock has committed before any OOS simulation.
            selections.append(dict(window=win["id"], parameters={k: asdict(v) for k, v in selected.items()}, locks=lock_ids))
            for family, candidate in selected.items():
                for scenario in STRESSES if family in FAMILIES else ("base",):
                    key = (family, scenario)
                    c = stressed(candidate, scenario)
                    account = accounts.get(key)
                    if account is not None:
                        # Costs are training-selected and take effect at window boundary.
                        account.costs = c.costs
                    try:
                        account, curve, risk, metrics = run_backtest(bundle, c, start=win["oos_start"], end=win["oos_end"],
                            code_version=version, cache=cache, account=account, scenario=scenario,
                            run_id=study_id + "/" + family + "/" + scenario)
                    except (ValueError, ArithmeticError) as exc:
                        # No replacing failed scenarios with fresh capital.
                        raise ValueError(f"OOS accounting/execution failure {win['id']}/{key}: {exc}") from exc
                    accounts[key] = account
                    curves.setdefault(key, []).extend(curve)
                    risk_rows.setdefault(key, []).extend(risk)
                    window_metrics.setdefault(key, []).append(metrics)
                    repo.checkpoint(study_id, win["id"], family, scenario, version, bundle.version, account)
                    repo.run(study_id, "window/" + win["id"] + "/" + family + "/" + scenario,
                             "replayed_oos_window", {"lock": lock_ids[family], "parameters": asdict(c)},
                             metrics, account, curve, risk)
            repo.record("walk_forward_windows", (study_id, win["id"]),
                        dict(window=win, locks=lock_ids, metrics={str(k): v[-1] for k, v in window_metrics.items()}))
            # Do not retain all training feature windows for the entire study.
            cache.cache.clear()
            print(f"{study_id} {win['id']} LOCKED -> OOS complete; shared account carried forward", flush=True)
        summary = {}
        initial = PortfolioAccount({s: research_contract(s) for s in CORE}, mode=ASSUMPTION).snapshot()
        for key, account in accounts.items():
            family, scenario = key
            metrics = performance(curves[key], initial)
            metrics["OOS_Windows"] = len(window_metrics[key])
            metrics["Positive_Windows"] = sum(m["total_return"] > 0 for m in window_metrics[key])
            metrics["scope"] = "replayed_oos"
            label = family + "/" + scenario
            repo.run(study_id, label, "replayed_oos", selections, metrics, account, curves[key], risk_rows[key])
            if scenario == "base":
                summary[family] = metrics
            if family in FAMILIES:
                repo.record("stress_tests", (study_id, family, scenario), metrics)
        benchmarks = {}
        begin, end = schedule[0]["oos_start"], schedule[-1]["oos_end"]
        for label, method, hold, single, target in [
                ("BTC Buy & Hold", "equal", True, "BTC", .12),
                ("ETH Buy & Hold", "equal", True, "ETH", .12),
                ("XAU Buy & Hold", "equal", True, "XAU", .12),
                ("XAG Buy & Hold", "equal", True, "XAG", .12),
                ("Passive Equal Weight", "equal", True, None, .12),
                ("Passive Inverse Vol", "inverse_vol", True, None, .50),
                ("Passive Risk Budget", "cluster", False, None, .12),
                ("Volatility Target Passive", "inverse_vol", False, None, .12)]:
            c = passive(method, target)
            account, curve, risk, metrics = run_backtest(bundle, c, start=begin, end=end, code_version=version,
                cache=cache, buy_hold=hold, single_asset=single, run_id=study_id + "/" + label)
            metrics.update(OOS_Windows=5, Positive_Windows=None, scope="fixed historical reference")
            repo.run(study_id, label, "BENCHMARK", asdict(c), metrics, account, curve, risk)
            repo.record("benchmarks", (study_id, label), metrics)
            benchmarks[label] = metrics
        acceptance = {}
        for family in FAMILIES:
            m = summary[family]
            stresses = {s: performance(curves[(family, s)], initial) for s in STRESSES}
            survival = all(v["total_return"] > 0 and v["MDD"] <= .40 for v in stresses.values())
            stable = not any(d["fragile"] for d in diagnostics if d["family"] == family)
            numerical = (m["Sharpe"] is not None and m["Sharpe"] >= .5 and m["MDD"] <= .30 and
                         m["Positive_Windows"] >= 3 and survival and stable)
            acceptance[family] = dict(status="CANDIDATE" if numerical else "REJECTED",
                numerical_gate=bool(numerical), stress_survival=bool(survival), neighborhood_stable=stable,
                formal_gate="FAIL", paper_trading=False, stress_metrics=stresses)
        report = dict(study_id=study_id, protocol=protocol, quality=bundle.quality,
            summary=summary, benchmarks=benchmarks, acceptance=acceptance,
            neighborhoods=diagnostics, selections=selections,
            trials=len(grid)*len(schedule), distinct_parameters=len(grid),
            failed_trials=sum(r["n"] for r in repo.store.rows("SELECT count(*) n FROM parameter_trials WHERE study_id=? AND status='FAILED'", (study_id,))),
            hard_gates={"look_ahead": "TEST_EVIDENCE_REQUIRED", "data_lineage": "FAIL_HISTORICAL_VINTAGE_CALENDAR_FX",
                        "execution_contract": "FAIL_UNVERIFIED", "checkpoint": "TEST_EVIDENCE_REQUIRED",
                        "accounting": "PASS_CONSERVATION_EVERY_STEP", "oos_leakage": "REPLAYED_NOT_UNTOUCHED"},
            elapsed_seconds=time.perf_counter()-start_clock, cache_hits=cache.hits, cache_misses=cache.misses,
            robustness="NOT_RUN: SPY legacy only; Treasury proxy unverified; Core not replaced",
            status="RESEARCH_COMPLETE_FORMAL_BLOCKED")
        repo.record("reviews", (study_id, "v1"), report)
        repo.finish(study_id, report["status"])
        print(f"{study_id} complete: {report['trials']} trials, {report['elapsed_seconds']:.1f}s", flush=True)
        return report
    except BaseException as exc:
        repo.record("reviews", (study_id, "failure"), {"type": type(exc).__name__, "error": str(exc)})
        repo.finish(study_id, "FAILED")
        raise
