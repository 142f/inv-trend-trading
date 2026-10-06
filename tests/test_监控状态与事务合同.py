"""监控状态必须能解释、重启、去重并原子提交。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from inv_trend.monitoring.模型 import DecisionSnapshot, EventKind, Phase, StateRecord
from inv_trend.monitoring.状态机 import Facts, transition
from inv_trend.monitoring.事件存储 import EventStore, StateConflictError


def facts(**kwargs):
    return Facts(**({'setup': False, 'candidate': False, 'close_confirmed': False,
                    'trend_allowed': True, 'execution_allowed': True,
                    'quality_allowed': True, 'fresh': True, 'direction': 1} | kwargs))


def decision(before, outcome, *, bar='2024-01-03T00:00:00+00:00', **kwargs):
    values = dict(
        instrument_id='BTCUSDT.SPOT', symbol=before.symbol, timeframe=before.timeframe,
        bar_time=bar, evaluated_at='2024-01-04T00:01:00+00:00', dataset_version='data-1',
        strategy_version='turtle-v1', config_hash='a' * 64, git_commit='b' * 40,
        code_hash='c' * 64, market={'open': 10., 'high': 12., 'low': 9., 'close': 11., 'volume': 100.},
        indicators={'atr': 1., 'channel': 10.},
        conditions={'quality_gate': True, 'freshness_gate': True, 'close_confirmation': True,
                    'trend_gate': True, 'execution_gate': True},
        state_before=before.phase, state_after=outcome.after.phase, reasons=outcome.reasons,
        event=outcome.event,
    )
    return DecisionSnapshot(**(values | kwargs))


def test_full_signal_and_actual_position_lifecycle():
    state = StateRecord('BTC', '1D')
    expected = [Phase.SETUP, Phase.CANDIDATE, Phase.CONFIRMED_ENTRY]
    for observation, target in zip([facts(setup=True), facts(candidate=True),
                                    facts(close_confirmed=True)], expected, strict=True):
        result = transition(state, observation)
        assert result.after.phase is target
        state = result.after
    # A repeated confirmation is still a market signal, never an invented fill.
    repeated = transition(state, facts(close_confirmed=True))
    assert repeated.after.phase is Phase.CONFIRMED_ENTRY
    assert repeated.after.position_ref is None and repeated.event is None
    held = transition(repeated.after, facts(position_open=True, position_ref='manual-pos-1'))
    assert held.after.phase is Phase.POSITION
    assert held.event.kind is EventKind.POSITION_EVENT
    exit_result = transition(held.after, facts(position_open=True, position_ref='manual-pos-1',
                                             exit_triggered=True, exit_code='STOP_EXIT'))
    assert exit_result.after.phase is Phase.EXIT and exit_result.event.code == 'STOP_EXIT'
    pending = transition(exit_result.after, facts(position_open=True, position_ref='manual-pos-1'))
    assert pending.after.phase is Phase.EXIT and pending.event is None
    flat = transition(pending.after, facts(position_open=False))
    assert flat.after.phase is Phase.IDLE and flat.after.position_ref is None
    assert flat.event.kind is EventKind.POSITION_EVENT


@pytest.mark.parametrize('flag, reason', [('quality_allowed', 'QUALITY_GATE_BLOCKED'),
                                        ('fresh', 'STALE_DATA')])
def test_unknown_market_health_preserves_position_and_suppresses_exit(flag, reason):
    before = StateRecord('BTC', 'H4', Phase.POSITION, 1, 'manual-pos-1')
    result = transition(before, facts(**{flag: False}, position_open=True,
                                    position_ref='manual-pos-1', exit_triggered=True))
    assert result.after.phase is Phase.POSITION and result.event is None
    assert reason in result.reasons


@pytest.mark.parametrize('flag', ['trend_allowed', 'execution_allowed'])
def test_confirmation_cannot_bypass_final_gate(flag):
    result = transition(StateRecord('BTC', 'D1'), facts(close_confirmed=True, **{flag: False}))
    assert result.after.phase is Phase.CANDIDATE


def test_candidate_invalidation_returns_to_setup_then_idle():
    state = StateRecord('BTC', 'D1', Phase.CANDIDATE, 1)
    assert transition(state, facts(setup=True)).after.phase is Phase.SETUP
    assert transition(state, facts()).after.phase is Phase.IDLE


@pytest.mark.parametrize('bad', [None, 'true', 1])
def test_unknown_fact_is_not_coerced_to_truth(bad):
    with pytest.raises(ValueError):
        facts(quality_allowed=bad)


def test_snapshot_cannot_certify_unknown_quality_or_nonfinite_evidence():
    before = StateRecord('BTC', 'D1')
    outcome = transition(before, facts(close_confirmed=True))
    with pytest.raises(ValueError):
        decision(before, outcome, conditions={'quality_gate': False})
    with pytest.raises(ValueError):
        decision(before, outcome, indicators={'atr': float('nan')})


def test_snapshot_is_deeply_immutable_and_clock_does_not_change_input_identity():
    before = StateRecord('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    raw = {'nested': {'values': [1, 2]}}
    first = decision(before, outcome, context=raw)
    raw['nested']['values'].append(3)
    assert first.context['nested']['values'] == (1, 2)
    with pytest.raises(TypeError):
        first.context['nested']['other'] = 4
    later = replace(first, evaluated_at='2024-01-04T01:01:00+00:00')
    assert first.input_key == later.input_key
    assert first.decision_id == later.decision_id


def test_commit_restart_and_duplicate_preserve_original_receipt(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    snapshot = decision(before, outcome)
    first = store.commit(snapshot, before, outcome.after)
    duplicate = EventStore(store.path).commit(replace(snapshot, evaluated_at='2024-01-04T01:00:00+00:00'),
                                             before, outcome.after)
    assert first['created'] is True and duplicate['created'] is False
    assert first['snapshot'] == duplicate['snapshot']
    assert store.state('BTC', 'D1').revision == 1
    assert len(store.rows('decisions')) == len(store.rows('events')) == len(store.rows('outbox')) == 1


def test_event_failure_rolls_back_snapshot_state_and_outbox(tmp_path, monkeypatch):
    store = EventStore(tmp_path / 'events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))

    def fail(db, snapshot):
        raise RuntimeError('event insert fault')

    monkeypatch.setattr(store, '_write_event', fail)
    with pytest.raises(RuntimeError, match='event insert fault'):
        store.commit(decision(before, outcome), before, outcome.after)
    restored = EventStore(store.path)
    assert restored.state('BTC', 'D1') == before
    for table in ('decisions', 'events', 'outbox'):
        assert restored.rows(table) == []


def test_concurrent_identical_scans_commit_one_event(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    snapshot = decision(before, outcome)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store.commit(snapshot, before, outcome.after), range(4)))
    assert sum(result['created'] for result in results) == 1
    assert len(store.rows('events')) == len(store.rows('outbox')) == 1


def test_stale_different_scan_does_not_overwrite_current(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    store.commit(decision(before, outcome), before, outcome.after)
    with pytest.raises(StateConflictError):
        store.commit(decision(before, outcome, bar='2024-01-04T00:00:00+00:00'), before, outcome.after)
    assert store.state('BTC', 'D1').revision == 1
    assert len(store.rows('decisions')) == 1


def test_timeframes_have_independent_state_and_events(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    for frame in ('D1', 'H4'):
        before = store.state('BTC', frame)
        outcome = transition(before, facts(setup=True))
        store.commit(decision(before, outcome), before, outcome.after)
    assert len(store.rows('events')) == 2
    assert len(store.rows('monitoring_state')) == 2


def test_event_store_cannot_adopt_an_unrelated_database(tmp_path):
    path = tmp_path / 'market-catalog.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE user_data(value TEXT)')
        db.execute("INSERT INTO user_data VALUES('preserve')")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        EventStore(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize('market', [{}, {'close': 11.},
                                  {'open': 10., 'high': 8., 'low': 9., 'close': 11., 'volume': 100.}])
def test_market_signal_needs_complete_valid_ohlcv(market):
    before = StateRecord('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    with pytest.raises(ValueError):
        decision(before, outcome, market=market)


def test_confirmed_state_cannot_bypass_gates_by_omitting_event():
    before = StateRecord('BTC', 'D1')
    outcome = transition(before, facts(close_confirmed=True))
    with pytest.raises(ValueError):
        decision(before, outcome, event=None, conditions={'quality_gate': False})
    with pytest.raises(ValueError):
        decision(before, outcome, event=None, market={})


def test_older_bar_cannot_rewind_monitoring_state(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(close_confirmed=True))
    store.commit(decision(before, outcome), before, outcome.after)
    current = store.state('BTC', 'D1')
    older_outcome = transition(current, facts())
    with pytest.raises(StateConflictError):
        store.commit(decision(current, older_outcome, bar='2024-01-02T00:00:00+00:00'),
                     current, older_outcome.after)
    assert store.state('BTC', 'D1') == current
