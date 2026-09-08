"""Independent persisted-evidence verification: locks, hashes, watermark, cash ledger."""
from __future__ import annotations

from collections import defaultdict
import json
import math
import zlib

from inv_trend.core.阶段协议_v1 import digest, timestamp
from inv_trend.core.在线策略_v1 import Candidate
from .阶段运行_v1 import StageRepository


def verify_study(data_root, study_id: str) -> dict:
    repository = StageRepository(data_root)
    store = repository.store
    issues = []
    def require(condition, message):
        if not condition:
            issues.append(message)
    studies = store.rows("SELECT * FROM stage_studies_v1 WHERE study_id=?", (study_id,))
    if not studies:
        raise KeyError(study_id)
    study = studies[0]
    protocol = json.loads(study["protocol_json"])
    require(study["status"] == "COMPLETE", "study not COMPLETE")
    require(digest(protocol) == study["protocol_hash"], "protocol hash mismatch")
    require(study["production_enabled"] == 0, "unauthorized production flag")
    trials = store.rows("SELECT * FROM stage_trials_v1 WHERE study_id=?", (study_id,))
    locks = store.rows("SELECT * FROM stage_locks_v1 WHERE study_id=?", (study_id,))
    candidates = store.rows("SELECT * FROM stage_candidates_v1 WHERE study_id=?", (study_id,))
    queries = [json.loads(r["payload_json"]) for r in store.rows(
        "SELECT * FROM stage_queries_v1 WHERE study_id=? ORDER BY ordinal", (study_id,))]
    n_windows = len(protocol["windows"]) * len(protocol["symbols"])
    require(len(candidates) == 36 and len({r["candidate_id"] for r in candidates}) == 36,
            "candidate grid incomplete")
    require(len(trials) == n_windows * 36 * 3, "training fold count incomplete")
    require(len(locks) == n_windows * 4, "parameter locks incomplete")
    by_lock = {(r["symbol"], r["family"], r["window_id"]): r for r in locks}
    for row in locks:
        window = {"id": row["window_id"], "cutoff": row["cutoff"], "start": row["test_start"], "end": row["test_end"]}
        selection = json.loads(row["selection_json"])
        require(digest({"window": window, "selection": selection,
                        "symbol": row["symbol"], "family": row["family"]}) == row["lock_hash"],
                "parameter lock hash mismatch")
        require(timestamp(row["cutoff"]) < timestamp(row["test_start"]) < timestamp(row["test_end"]),
                "invalid training/OOS temporal boundary")
        stream = [q for q in queries if q["kind"] == "STREAM" and q["symbol"] == row["symbol"]
                  and q["start"] == row["test_start"] and q["end_exclusive"] == row["test_end"]]
        require(len(stream) == 1, "missing/duplicate current-window stream")
        if stream:
            require("requested_at" in stream[0] and row["committed_at"] <= stream[0].get("requested_at", ""),
                    "market cursor opened before parameter lock committed")
    for q in queries:
        require(timestamp(q["end_exclusive"]) <= timestamp(protocol["holdout_start"]), "holdout query detected")
        if q["max_available_at"]:
            require(timestamp(q["max_available_at"]) < timestamp(q["end_exclusive"]), "future row consumed")
    for row in trials:
        key = next((k for k in by_lock if k[0] == row["symbol"] and k[2] == row["window_id"]), None)
        require(key is not None and row["train_end"] <= by_lock[key]["cutoff"], "training consumed OOS data")
    signals = store.rows("SELECT * FROM stage_decisions_v1 WHERE study_id=?", (study_id,))
    prices = {}
    for row in signals:
        value = json.loads(zlib.decompress(row["payload"]))
        require(digest(value) == row["output_hash"], "signal evidence hash mismatch")
        b = value["features"]["bar"]
        require(b["available_at"] == row["as_of"] and b["symbol"] == row["symbol"], "signal bar identity mismatch")
        lock = by_lock.get((row["symbol"], row["family"], row["window_id"]))
        selection = json.loads(lock["selection_json"]) if lock else {}
        require(lock is not None and "parameters" in selection and row["candidate_id"] == Candidate(**selection["parameters"]).id,
                "signal used unlocked parameters")
        prices[(row["symbol"], row["as_of"])] = b["close"]
    fills = store.rows("SELECT * FROM stage_fills_v1 WHERE study_id=? ORDER BY filled_at", (study_id,))
    fills_by_account = defaultdict(list)
    for row in fills:
        fill = json.loads(row["payload_json"])
        require(timestamp(fill["signal_at"]) < timestamp(fill["filled_at"]), "same-close/future fill detected")
        require(fill["fee"] >= 0 and fill["slippage_cost"] >= 0, "negative execution costs")
        fills_by_account[(row["symbol"], row["family"], row["scenario"])].append(fill)
    curves = store.rows("SELECT * FROM stage_curves_v1 WHERE study_id=? ORDER BY as_of", (study_id,))
    by_account = defaultdict(list)
    for row in curves:
        by_account[(row["symbol"], row["family"], row["scenario"])].append(row)
    for key, path in by_account.items():
        cash = protocol["execution"]["initial_cash"]
        quantity, fees, slippage, index = 0.0, 0.0, 0.0, 0
        f = fills_by_account[key]
        for point in path:
            while index < len(f) and f[index]["filled_at"] <= point["as_of"]:
                event = f[index]
                cash -= event["quantity"] * event["price"] + event["fee"]
                quantity += event["quantity"]
                fees += event["fee"]
                slippage += event["slippage_cost"]
                index += 1
            price = prices.get((key[0], point["as_of"]))
            require(price is not None, "missing decision/price evidence")
            require(cash >= -1e-6 and quantity >= -1e-9, "capital/short-sale constraint violation")
            require(math.isclose(cash, point["cash"], abs_tol=1e-6, rel_tol=1e-10), "cash ledger mismatch")
            require(math.isclose(quantity, point["quantity"], abs_tol=1e-8), "position ledger mismatch")
            require(math.isclose(fees, point["fees"], abs_tol=1e-6), "fee ledger mismatch")
            require(math.isclose(slippage, point["slippage_cost"], abs_tol=1e-6), "slippage ledger mismatch")
            if price is not None:
                require(math.isclose(cash + quantity * price, point["equity"], rel_tol=1e-10, abs_tol=1e-6),
                        "equity does not reconcile to cash plus marked position")
    results = store.rows("SELECT * FROM stage_window_results_v1 WHERE study_id=? ORDER BY window_id", (study_id,))
    require(len(results) == n_windows * 10, "OOS/stress/baseline window count incomplete")
    previous = {}
    for row in results:
        key = (row["symbol"], row["family"], row["scenario"])
        require(math.isclose(row["start_equity"], previous.get(key, protocol["execution"]["initial_cash"]),
                             rel_tol=1e-10, abs_tol=1e-6), "window capital was reset or discontinuous")
        previous[key] = row["end_equity"]
    artifacts = store.rows("SELECT * FROM stage_artifacts_v1 WHERE substr(run_id,1,?)=?", (len(study_id) + 1, study_id + "/"))
    for row in artifacts:
        try:
            packet = repository.load(row["artifact_id"])
            require(packet.context.dataset_version == study["dataset_version"], "artifact data version mismatch")
            require(packet.context.code_version == study["code_version"], "artifact code version mismatch")
            if packet.stage in ("evaluate", "report"):
                matching = next((r for r in results if packet.context.run_id ==
                    f"{study_id}/{r['symbol']}/{r['family']}/{r['window_id']}/{r['scenario']}"), None)
                if matching:
                    stage_metrics = dict(packet.payload.metrics)
                    summary_metrics = json.loads(matching["metrics_json"])
                    for name in ("total_return", "annualized_return", "max_drawdown", "sharpe_ratio",
                                 "fees", "slippage_cost", "terminal_equity", "bars", "fill_count"):
                        left, right = stage_metrics[name], summary_metrics[name]
                        require(left is None and right is None or left is not None and right is not None
                                and math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-8),
                                "stage/summary metric mismatch: " + name)
            for parent_id in packet.parent_ids:
                parent = repository.load(parent_id)
                require(packet.input_hash == parent.output_hash, "upstream payload hash mismatch")
                require(parent.context == packet.context, "cross-run stage linkage")
        except (ValueError, KeyError) as exc:
            issues.append(f"artifact verification failed: {exc}")
    review_rows = store.rows("SELECT * FROM stage_reviews_v1 WHERE study_id=?", (study_id,))
    for row in review_rows:
        require(digest(json.loads(row["payload_json"])) == row["content_hash"], "review hash mismatch")
    return {"ok": not issues, "study_id": study_id, "issues": sorted(set(issues)),
            "checked": {"candidates": len(candidates), "training_folds": len(trials),
                        "locks": len(locks), "queries": len(queries), "decisions": len(signals),
                        "fills_including_rejections": len(fills), "curve_points": len(curves),
                        "windows_including_stress_baseline": len(results), "stage_artifacts": len(artifacts)},
            "production_enabled": False,
            "scope": "persisted evidence consistency; not market-source certification"}
