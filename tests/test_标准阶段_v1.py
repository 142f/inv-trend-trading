from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

import pytest

from inv_trend.core.阶段协议_v1 import (
    Bar, RawBar, FeatureFrame, Signal, RunContext, Quality, emit, digest,
    encode_envelope, decode_envelope,
)
from inv_trend.core.在线策略_v1 import Candidate, OnlineFeatures, OnlineStrategy, candidate_grid
from inv_trend.core.事件账户_v1 import CashBroker, ExecutionConfig
from inv_trend.application.标准阶段_v1 import stages, ProcessingStage, IndicatorStage, EvaluationStage
from inv_trend.application.研究评分_v1 import training_score, equity_metrics
from inv_trend.application.多方法研究_v1 import train_grid
from inv_trend.storage.阶段运行_v1 import StageRepository


def bar(i, *, close=None, opened=None, volume=1_000_000.):
    price = close if close is not None else 100 + 0.12 * i + 5 * math.sin(i / 8)
    op = opened if opened is not None else price * 0.998
    day = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
    return Bar("QQQ", "D1", "fixture", (day + timedelta(hours=9)).isoformat(),
               (day + timedelta(hours=16)).isoformat(), op, max(op, price)*1.01,
               min(op, price)*.99, price, volume, i)


def signal(b, weight=.8):
    f = FeatureFrame(b, (("volatility", .15),), True, "fixture",
                     (("lookback", 20), ("exit_lookback", 10)))
    return Signal(f, 1 if weight > 0 else -1 if weight < 0 else 0, weight, "fixture", ("test",))


def context(run="test"):
    return RunContext(run, "dataset-fixture-v1", "strategy-fixture-v1", "code-fixture-v1",
                      digest({"fixture": True}), "TEST")


def pipeline(bars, *, candidate=None, execution=None):
    chain = stages(context(), candidate or Candidate("momentum", 20), execution or ExecutionConfig())
    result = []
    for b in bars:
        p = chain[0].run(RawBar(**asdict(b)))
        row = [p]
        for st in chain[1:]:
            p = st.run(p)
            row.append(p)
        result.append(row)
    return chain, result


@pytest.mark.parametrize("key,value", [("close", float("nan")), ("volume", -1), ("low", -1),
                                       ("open", 0), ("high", 1), ("sequence", -1)])
def test_bar_schema_fails_closed(key, value):
    with pytest.raises(ValueError):
        replace(bar(0), **{key: value})


@pytest.mark.parametrize("value", ["2020-01-01T00:00:00", "2020-01-01T08:00:00+08:00"])
def test_utc_required(value):
    with pytest.raises(ValueError):
        replace(bar(0), opened_at=value)


def test_36_distinct_candidates_and_three_families():
    grid = candidate_grid()
    assert len(grid) == len({c.id for c in grid}) == 36
    assert {c.family for c in grid} == {"momentum", "breakout", "reversion"}
    assert all(sum(c.family == f for c in grid) == 12 for f in {c.family for c in grid})


@pytest.mark.parametrize("family", ["momentum", "breakout", "reversion"])
def test_all_seven_stages_roundtrip_and_composition(family):
    c = Candidate(family, 20, threshold=1.5 if family == "reversion" else .01)
    _, rows = pipeline([bar(i) for i in range(65)], candidate=c)
    assert [p.stage for p in rows[-1]] == ["acquire", "process", "indicators", "signal", "backtest", "evaluate", "report"]
    for p in rows[-1]:
        q = decode_envelope(encode_envelope(p))
        assert p.artifact_id == q.artifact_id
        assert p.payload == q.payload
    assert rows[-1][-1].payload.production_enabled is False


def test_payload_tampering_rejected():
    _, rows = pipeline([bar(0)])
    p = replace(rows[0][1], payload=replace(bar(0), volume=123.0))
    with pytest.raises(ValueError, match="integrity"):
        p.validate()


def test_metadata_tampering_changes_identity():
    _, rows = pipeline([bar(0)])
    p = rows[0][1]
    assert replace(p, context=replace(p.context, dataset_version="evil")).artifact_id != p.artifact_id


def test_unknown_schema_extra_fields_rejected():
    _, rows = pipeline([bar(0)])
    raw = json.loads(encode_envelope(rows[0][0]))
    raw["envelope"]["unexpected"] = True
    with pytest.raises(ValueError):
        decode_envelope(json.dumps(raw))


def test_wrong_stage_or_context_rejected():
    _, rows = pipeline([bar(0)])
    stage = IndicatorStage(Candidate("momentum", 20))
    with pytest.raises(ValueError):
        stage.run(rows[0][0])
    stage.run(rows[0][1])
    packet = replace(rows[0][1], context=context("other-run"))
    with pytest.raises(ValueError, match="mix run"):
        stage.run(packet)


def test_duplicate_and_unsorted_streams_rejected():
    with pytest.raises(ValueError):
        pipeline([bar(1), bar(1)])
    with pytest.raises(ValueError):
        pipeline([bar(1), bar(0)])


def test_no_trade_at_signal_close_and_gap_capital_constrained():
    broker = CashBroker(ExecutionConfig(initial_cash=10000, fee_bps=20, slippage_bps=10, max_weight=1))
    first = broker.on_signal(signal(bar(0, close=100), 1))
    assert first.quantity == 0 and first.fills == ()
    second = broker.on_signal(signal(bar(1, opened=200, close=201), 1))
    assert second.fills[0].price == pytest.approx(200 * 1.001)
    assert 0 < second.quantity < 100
    assert second.cash >= 0
    assert second.fills[0].filled_at > second.fills[0].signal_at
    assert second.fees == pytest.approx(second.quantity * 200 * 1.001 * .002)


def test_current_future_close_high_low_and_volume_do_not_affect_open_fill():
    a, b = CashBroker(ExecutionConfig()), CashBroker(ExecutionConfig())
    a.on_signal(signal(bar(0, close=100, volume=500)))
    b.on_signal(signal(bar(0, close=100, volume=500)))
    left = a.on_signal(signal(bar(1, opened=105, close=106, volume=1)))
    right = b.on_signal(signal(bar(1, opened=105, close=250, volume=1e12)))
    assert left.fills == right.fills
    assert left.quantity == 5  # previous-day 500 * 1% participation, not current full-day volume


@pytest.mark.parametrize("delay", [1, 2, 3])
def test_execution_delay(delay):
    broker = CashBroker(ExecutionConfig(delay_bars=delay))
    rows = [broker.on_signal(signal(bar(i))) for i in range(delay+2)]
    assert all(row.quantity == 0 for row in rows[:delay])
    assert rows[delay].quantity > 0


@pytest.mark.parametrize("config", [{"delay_bars": 0}, {"volume_participation": 2}, {"lot_size": 0},
                                    {"fee_bps": -1}, {"initial_cash": 0}, {"slippage_bps": 10000}])
def test_invalid_execution_parameters(config):
    with pytest.raises(ValueError):
        ExecutionConfig(**config)


def test_zero_known_volume_blocks_fills():
    broker = CashBroker(ExecutionConfig())
    broker.on_signal(signal(bar(0, volume=0)))
    result = broker.on_signal(signal(bar(1, volume=1e12)))
    assert result.quantity == 0
    assert result.fills[0].status == "REJECTED"


def test_short_signal_cannot_create_unbacked_borrow():
    broker = CashBroker(ExecutionConfig())
    for i in range(5):
        result = broker.on_signal(signal(bar(i), -.9))
    assert result.quantity == 0 and result.equity == 100000
    assert broker.short_signals_blocked == 5


@pytest.mark.parametrize("family", ["momentum", "breakout", "reversion"])
def test_future_suffix_perturbation_does_not_change_past(family):
    c = Candidate(family, 20, threshold=1.5 if family == "reversion" else .01)
    base = [bar(i) for i in range(100)]
    changed = base[:65] + [bar(i, close=bar(i).close * 4) for i in range(65, 100)]
    _, left = pipeline(base, candidate=c)
    _, right = pipeline(changed, candidate=c)
    assert [[p.artifact_id for p in row] for row in left[:65]] == [[p.artifact_id for p in row] for row in right[:65]]


def test_breakout_uses_prior_high_not_same_bar_high():
    c = Candidate("breakout", 20)
    features = OnlineFeatures(c)
    strategy = OnlineStrategy(c)
    for i in range(22):
        features.update(bar(i, close=100, opened=100))
    today = bar(22, close=110, opened=100)
    f = features.update(today)
    assert f.value("channel_high") == 101.0
    assert strategy.update(f).direction == 1


def test_all_stage_checkpoint_restore_is_exact():
    c = Candidate("breakout", 20)
    original, rows = pipeline([bar(i) for i in range(50)], candidate=c)
    restored = stages(context(), c, ExecutionConfig())
    for a, b in zip(original, restored):
        b.restore(json.loads(json.dumps(a.snapshot())))
    for i in range(50, 65):
        x, y = RawBar(**asdict(bar(i))), RawBar(**asdict(bar(i)))
        for a, b in zip(original, restored):
            x, y = a.run(x), b.run(y)
            assert x.artifact_id == y.artifact_id


def test_broker_checkpoint_continuity_and_no_terminal_liquidation():
    broker = CashBroker(ExecutionConfig())
    for i in range(30):
        old = broker.on_signal(signal(bar(i)))
    other = CashBroker(broker.config)
    other.restore(json.loads(json.dumps(broker.snapshot())))
    assert broker.on_signal(signal(bar(30))) == other.on_signal(signal(bar(30)))
    assert old.quantity > 0 and old.pending_orders > 0


def test_audited_and_fast_training_paths_are_economically_identical():
    c = Candidate("momentum", 20)
    bars = [bar(i) for i in range(80)]
    _, standard = pipeline(bars, candidate=c)
    f, s, broker = OnlineFeatures(c), OnlineStrategy(c), CashBroker(ExecutionConfig())
    fast = [broker.on_signal(s.update(f.update(b, trace=False)), audit=False) for b in bars]
    for row, snap in zip(standard, fast):
        observed = row[4].payload
        assert (observed.cash, observed.quantity, observed.equity, observed.fees) == (
                snap.cash, snap.quantity, snap.equity, snap.fees)


def test_training_cache_is_exact_and_selection_rejects_oos():
    bars = tuple(bar(i) for i in range(220))
    candidates = candidate_grid()[:4]
    a = train_grid(bars, candidates, ExecutionConfig(), warmup=60, cache_features=True)
    b = train_grid(bars, candidates, ExecutionConfig(), warmup=60, cache_features=False)
    assert a == b
    with pytest.raises(ValueError, match="TRAIN"):
        training_score([{"scope": "OOS", "metrics": a[1][0]["metrics"]}])


def test_cash_only_training_does_not_win_from_zero_volatility():
    metric = equity_metrics([100000.] * 100)
    score, eligible = training_score([{"scope": "TRAIN", "metrics": metric}]*3)
    assert not eligible and score < 0


def test_drawdown_and_first_bar_loss_include_initial_capital():
    metrics = equity_metrics([90000, 99000], initial=100000)
    assert metrics["max_drawdown"] == pytest.approx(.1)
    assert metrics["total_return"] == pytest.approx(-.01)


def test_sql_publication_is_atomic_idempotent_and_recovers_state(tmp_path):
    store = StageRepository(tmp_path / "data", create=True)
    chain, rows = pipeline([bar(0)])
    packets = rows[0]
    with pytest.raises(sqlite3.IntegrityError):
        store.publish([packets[1]])  # missing upstream FK: entire publication must roll back
    assert store.store.rows("SELECT count(*) AS n FROM stage_artifacts_v1")[0]["n"] == 0
    checkpoints = [("test", s.name, packets[-1].as_of, s.snapshot()) for s in chain]
    store.publish(packets, checkpoints)
    store.publish(packets, checkpoints)
    assert store.store.rows("SELECT count(*) AS n FROM stage_artifacts_v1")[0]["n"] == 7
    for p in packets:
        assert store.load(p.artifact_id).payload == p.payload
    assert store.checkpoint("test", "backtest", packets[-1].as_of) == chain[4].snapshot()
    bad = [("test", "backtest", packets[-1].as_of, {"changed": True})]
    with pytest.raises(ValueError, match="immutable"):
        store.publish([], bad)


def test_parameter_freeze_cannot_be_overwritten(tmp_path):
    store = StageRepository(tmp_path / "data", create=True)
    protocol = {"dataset_version": "fixture", "code_version": "test"}
    store.freeze("研究_v1", protocol, candidate_grid())
    with pytest.raises(sqlite3.IntegrityError):
        store.freeze("研究_v1", {**protocol, "changed": True}, candidate_grid())
    assert store.store.rows("SELECT count(*) n FROM stage_candidates_v1")[0]["n"] == 36


def test_schema_checksum_must_match(tmp_path):
    store = StageRepository(tmp_path / "data", create=True)
    with store.store.connect() as db:
        db.execute("UPDATE stage_schema_v1 SET schema_hash='tampered'")
    with pytest.raises(ValueError, match="checksum"):
        StageRepository(tmp_path / "data")


def test_stage_failure_is_structured_serializable_and_restores_state():
    from inv_trend.application.标准阶段_v1 import run_checked
    _, rows = pipeline([bar(0)])
    indicator = IndicatorStage(Candidate("momentum", 20))
    before = indicator.snapshot()
    result = run_checked(indicator, rows[0][0])
    assert result.quality.status == "FAIL"
    assert result.quality.errors
    assert indicator.snapshot() == before
    assert decode_envelope(encode_envelope(result)).quality.status == "FAIL"
    with pytest.raises(ValueError, match="upstream quality failed"):
        result.validate()


def test_feature_parameter_swap_rejected():
    f = OnlineFeatures(Candidate("momentum", 60))
    features = f.update(bar(0))
    with pytest.raises(ValueError, match="parameter contract"):
        OnlineStrategy(Candidate("momentum", 20)).update(features)


def test_equivalent_utc_spellings_canonicalized():
    original = bar(0)
    replaced = replace(original, available_at=original.available_at.replace("+00:00", "Z"))
    assert original == replaced


def test_entry_exposure_cap_uses_post_fee_equity():
    execution = ExecutionConfig(initial_cash=100000, fee_bps=100, slippage_bps=100,
                                max_weight=.95, lot_size=.001)
    broker = CashBroker(execution)
    broker.on_signal(signal(bar(0, close=100, opened=100), .95))
    result = broker.on_signal(signal(bar(1, close=100, opened=100), .95))
    assert result.quantity * 100 / result.equity <= .95 + 1e-9


@pytest.mark.parametrize("column", ["date", "time"])
def test_legacy_csv_helper_single_read_preserves_schema(tmp_path, monkeypatch, column):
    import importlib.util
    from pathlib import Path
    import pandas as pd
    path = Path(__file__).resolve().parents[1] / "research/d1_suite/scripts/d1_backtest_common.py"
    spec = importlib.util.spec_from_file_location("legacy_fixture_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = tmp_path / "行情.csv"
    fixture.write_text(f"{column},open,high,low,close,volume,spread\n"
                       "2020-01-03,10,12,9,11,1000,0\n"
                       "2020-01-02,9,11,8,10,800,0\n")
    original, calls = pd.read_csv, []
    def traced(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, "read_csv", traced)
    result = module.load_csv(fixture)
    assert len(calls) == 1
    assert list(result.columns) == ["open", "high", "low", "close", "volume", "spread"]
    assert str(result.index.tz) == "UTC" and result.index.is_monotonic_increasing
    assert result.close.tolist() == [10, 11]


def test_research_and_storage_entrypoints_import_and_help(capsys):
    from inv_trend.cli.分阶段研究_v1 import main as research_main
    from inv_trend.cli.存储治理_v1 import main as storage_main
    for main in (research_main, storage_main):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0


@pytest.mark.parametrize("candidate", [Candidate("momentum", 20), Candidate("breakout", 20), Candidate("reversion", 20, threshold=1.5)])
def test_streaming_and_summary_metric_definitions_are_identical(candidate):
    from inv_trend.application.研究评分_v1 import snapshot_metrics
    chain, rows = pipeline([bar(i) for i in range(160)], candidate=candidate)
    expected = snapshot_metrics([r[4].payload for r in rows])
    actual = dict(rows[-1][5].payload.metrics)
    for name in ("total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "fees", "slippage_cost", "fill_count"):
        if expected[name] is None:
            assert actual[name] is None
        else:
            assert actual[name] == pytest.approx(expected[name], rel=1e-9, abs=1e-9)


def test_window_evaluation_costs_use_window_start_not_zero():
    from inv_trend.application.研究评分_v1 import snapshot_metrics
    _, rows = pipeline([bar(i) for i in range(160)])
    previous = rows[79][4].payload
    stage = EvaluationStage(initial_equity=previous.equity, initial_fees=previous.fees,
                            initial_slippage=previous.slippage_cost)
    for r in rows[80:]:
        result = stage.run(r[4])
    expected = snapshot_metrics([r[4].payload for r in rows[80:]], initial=previous.equity,
                               initial_fees=previous.fees, initial_slippage=previous.slippage_cost)
    for name in ("total_return", "annualized_return", "fees", "slippage_cost"):
        assert dict(result.payload.metrics)[name] == pytest.approx(expected[name])


def test_standalone_stage_does_not_silently_use_wrong_parameters(tmp_path):
    from inv_trend.cli.分阶段研究_v1 import resolve_configuration
    from inv_trend.application.多方法研究_v1 import code_hash
    candidate = Candidate("momentum", 20, threshold=.03)
    ctx = RunContext("run", "fixture", "v1", code_hash(), digest(candidate))
    p = emit(ctx, "acquire", RawBar(**asdict(bar(0))), bar(0).available_at)
    repo = StageRepository(tmp_path / "data", create=True)
    resolved, execution = resolve_configuration(repo, p)
    assert resolved == candidate
    with pytest.raises(ValueError, match="锁定参数"):
        resolve_configuration(repo, p, Candidate("momentum", 20).id)
    with pytest.raises(ValueError, match="代码版本"):
        resolve_configuration(repo, replace(p, context=replace(ctx, code_version="wrong")))


def test_standalone_configuration_rejects_corrupted_checkpoint(tmp_path):
    from inv_trend.cli.分阶段研究_v1 import resolve_configuration
    from inv_trend.application.多方法研究_v1 import code_hash
    candidate = Candidate("momentum", 20)
    ctx = RunContext("run", "fixture", "v1", code_hash(), digest(candidate))
    p = emit(ctx, "acquire", RawBar(**asdict(bar(0))), bar(0).available_at)
    repo = StageRepository(tmp_path / "data", create=True)
    with repo.store.connect() as db:
        db.execute("INSERT INTO stage_checkpoints_v1 VALUES(?,?,?,?,?)", (
            ctx.run_id,"signal",bar(0).available_at,"wrong-hash",json.dumps({"engine":{"candidate":asdict(candidate)}})))
    with pytest.raises(ValueError, match="完整性"):
        resolve_configuration(repo,p)
