from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.perpetual_audit.研究协议 import schedule, candidates, neighborhood, ExperimentRegistry
from inv_trend.application.perpetual_audit.统计验证 import paired_bootstrap, deflated_sharpe, concentration, candidate_decision
from inv_trend.core.永续风控 import (InstrumentSpec, PositionTier, IsolatedPosition, MarginStateMachine,
                                 funding_cashflow, specification_at, risk_sized_contracts, volatility_scale)
from inv_trend.core.永续风控 import settlement_position
from inv_trend.application.perpetual_audit.证据等级 import evidence_level
from inv_trend.data.永续历史 import OKXHistory, inspect_frame


def spec(**kwargs):
    return replace(InstrumentSpec('BTC-USDT-SWAP', 'v1', 't1', '2020-01-01T00:00:00Z', None,
                                  'fixture-only', 'SIMULATED_SPEC', 1., .001, .001, 100., .001, .002,
                                  (PositionTier(10000, .01, 100), PositionTier(20000, .02, 50),
                                   PositionTier(1000000, .03, 20))), **kwargs)


def test_windows_preserve_frozen_dates_and_preheat():
    index = pd.date_range('2019-01-01', '2026-09-06', tz='UTC')
    rows, status = schedule(index, '2022-03-01', track='execution')
    validation = [r for r in rows if r['stage'] == 'VALIDATION']
    assert status == 'READY' and len(validation) == 4
    assert validation[0]['start'] == '2024-02-29'
    assert validation[-1]['end'] == '2026-02-17'
    assert rows[-1]['sample_status'] == 'CONTAMINATED_TEST'


def test_short_history_does_not_shorten_training():
    rows, status = schedule(pd.date_range('2020-01-01', '2026-09-06', tz='UTC'), '2023-07-01')
    validation = [r for r in rows if r['stage'] == 'VALIDATION']
    assert status == 'INSUFFICIENT_FOLDS' and len(validation) == 1
    assert (validation[0]['start'], validation[0]['end']) == ('2025-06-30', '2025-12-26')


def test_future_extension_does_not_change_development_windows():
    old, _ = schedule(pd.date_range('2019-01-01', '2026-04-20', tz='UTC'))
    new, _ = schedule(pd.date_range('2019-01-01', '2026-09-06', tz='UTC'))
    assert old == [r for r in new if r['stage'] != 'TEST']


def test_missing_days_and_unproven_unseen_fail():
    index = pd.date_range('2019-01-01', '2026-09-06', tz='UTC')
    with pytest.raises(ValueError, match='every complete'):
        schedule(index.delete(100))
    with pytest.raises(ValueError, match='provenance'):
        schedule(index, sample_status='UNSEEN_TEST')


def test_registry_counts_repeat_separately_and_detects_tampering(tmp_path):
    journal = ExperimentRegistry(tmp_path / '实验登记.jsonl')
    for status in ('FAILED', 'COMPLETED'):
        run = journal.start({'a': 1}, code_hash='c', data_hash='d', sample_status='DEVELOPMENT')
        journal.finish(run, status)
    assert journal.summary()['total_runs'] == 2
    assert journal.summary()['total_trials'] == 1
    content = journal.path.read_text(encoding='utf-8').replace('COMPLETED', 'HIDDEN')
    journal.path.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        journal.read()


def test_twelve_centers_and_diagnostic_neighbors():
    assert len(candidates()) == 12
    assert all(c['role'] == 'DIAGNOSTIC_ONLY' for c in neighborhood(candidates()[-1]))


def test_historical_spec_boundary_and_overlap():
    a = spec(effective_to='2022-01-01T00:00:00Z')
    b = spec(instrument_spec_version='v2', effective_from='2022-01-01T00:00:00Z')
    assert specification_at([a, b], a.instrument, pd.Timestamp('2022-01-01T00:00:00Z')) == b
    with pytest.raises(ValueError, match='overlapping'):
        specification_at([spec(), b], a.instrument, pd.Timestamp('2022-01-01T00:00:00Z'))
    assert b.leverage(1000) == 3


def test_margin_state_partial_recalculates_lower_tier():
    # No price PnL: tier 3 is insolvent, reduction lowers the requirement.
    position = IsolatedPosition(1, 300., 100., 900.)
    result, events = MarginStateMachine().process(position, 100., spec())
    assert events[0]['state'] == 'CANCEL_OPEN_ORDERS'
    assert any(e['state'] == 'PARTIAL_LIQUIDATION' for e in events)
    assert result.contracts == 200
    assert MarginStateMachine.ratio(result, 100., spec()) > 1


def test_gap_can_go_directly_to_full_liquidation():
    position = IsolatedPosition(1, 50., 100., 100.)
    result, events = MarginStateMachine().process(position, 50., spec())
    assert result.contracts == 0
    assert [e['state'] for e in events] == ['CANCEL_OPEN_ORDERS', 'FULL_LIQUIDATION']


def test_funding_direction_and_actual_event_size():
    assert funding_cashflow(IsolatedPosition(1, 2, 100, 100), .001, 100, spec()) == -.2
    assert funding_cashflow(IsolatedPosition(-1, 2, 100, 100), .001, 100, spec()) == .2
    assert funding_cashflow(IsolatedPosition(1, 0, 100, 100), .001, 100, spec()) == 0


def test_stop_budget_not_one_n_and_no_free_margin():
    args = dict(equity=100000, side=1, price=100, spec=spec())
    q1 = risk_sized_contracts(**args, stop=98)
    q2 = risk_sized_contracts(**args, stop=96)
    assert q2 < q1
    assert q1 * (2 + .2) <= 500 + 1e-8
    assert risk_sized_contracts(**args, stop=98, margin_used=80000) == 0
    assert risk_sized_contracts(**args, stop=98, portfolio_stop_used=2000) == 0


def test_stress_does_not_cancel_long_short_risk():
    scale = volatility_scale([1., -1.], [[.25, .25], [.25, .25]], [.5, .5])
    assert scale == pytest.approx(.15)


def test_paired_identical_strategy_has_zero_delta_and_never_passes():
    rng = np.random.default_rng(4)
    folds = [rng.normal(.001, .02, 180) for _ in range(3)]
    result = paired_bootstrap(folds, folds, repetitions=100)
    assert result['sharpe'] == dict(delta=0., lower=0., upper=0.)
    assert candidate_decision(folds, trades=50, liquidations=0, comparison=result, complete_execution=True)[0] != 'E1'


def test_dsr_selection_penalty_and_concentration():
    r = np.random.default_rng(2).normal(.001, .01, 1000)
    a = deflated_sharpe(r, trials=1, sharpe_variance=.01)
    b = deflated_sharpe(r, trials=100, sharpe_variance=.01)
    assert b['deflated_sharpe'] < a['deflated_sharpe']
    ledger = concentration([1000, 10, -20], initial_equity=10000, ending_equity=10990)
    assert ledger['return_without_top_1'] == pytest.approx(-.001)
    assert ledger['status'] == 'EVIDENCE_INSUFFICIENT'


def test_history_pagination_and_error_manifest(tmp_path):
    batches = [dict(code='0', data=[['1577923200000', '1', '2', '1', '2', '3', '0', '0', '1']]),
               dict(code='0', data=[])]
    def transport(endpoint, params):
        return batches.pop(0)
    frame, manifest = OKXHistory(tmp_path, transport=transport).download('BTC-USDT-SWAP', 'daily')
    assert len(frame) == 1 and manifest['status'] == 'ENDPOINT_EXHAUSTED'
    assert manifest['inception_verified'] is False
    def broken(endpoint, params):
        raise ConnectionError('offline')
    _, manifest = OKXHistory(tmp_path, transport=broken).download('ETH-USDT-SWAP', 'daily')
    assert manifest['status'] == 'DOWNLOAD_ERROR'
    assert json.loads(next((tmp_path / 'ETH-USDT-SWAP/daily').glob('*清单.json')).read_text(encoding='utf-8'))['error']


def test_quality_checks_do_not_fill_missing_minutes():
    frame = pd.DataFrame({'timestamp': pd.to_datetime(['2024-01-01', '2024-01-03'], utc=True),
                          'open': [1., 1.], 'high': [2., 2.], 'low': [1., 1.], 'close': [2., 2.]})
    assert inspect_frame(frame, frequency='D')['gaps'] == 1


@pytest.mark.parametrize('offset,expected', [(-1, 0), (1, 2)])
def test_funding_before_after_one_millisecond(offset, expected):
    timestamp = pd.Timestamp('2024-01-01T08:00:00Z')
    fills = [dict(timestamp=timestamp + pd.Timedelta(milliseconds=offset), signed_contract_delta=-2)]
    assert settlement_position(2, fills, timestamp) == expected


def test_funding_exact_sequence_or_explicit_ambiguity():
    timestamp = pd.Timestamp('2024-01-01T08:00:00Z')
    fills = [dict(timestamp=timestamp, signed_contract_delta=-2, sequence=10)]
    assert settlement_position(2, fills, timestamp, sequence=11) == 0
    assert settlement_position(2, fills, timestamp, sequence=9) == 2
    with pytest.raises(ValueError, match='ambiguous'):
        settlement_position(2, fills, timestamp)


def test_evidence_does_not_promote_contaminated_or_short_paper():
    assert evidence_level(execution_complete=True, development_supported=True,
                          test={'sample_status': 'CONTAMINATED_TEST'})[0] == 'E1'
    test = dict(sample_status='UNSEEN_TEST', provenance='frozen record', trade_count=30,
                liquidations=0, max_drawdown=.1, net_return=.1, sharpe_delta=.2,
                sharpe_delta_ci_lower=.01, calmar_delta=.1, max_drawdown_delta=-.01)
    paper = dict(sample_status='PAPER_FORWARD', live_record_provenance='append-only record',
                 started_at='2026-01-01T00:00:00Z', last_observation_at='2026-02-01T00:00:00Z',
                 trade_count=100)
    assert evidence_level(execution_complete=True, development_supported=True, test=test, paper=paper)[0] == 'E2'
