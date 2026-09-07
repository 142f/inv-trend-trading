from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.perpetual_audit.分钟执行 import MinuteReplay
from inv_trend.application.perpetual_audit.研究协议 import candidates
from inv_trend.adapters.multi_asset.models.domain import Order
from tests.test_永续研究协议 import spec


def replay(scenario='worst_case', funding=None):
    symbol = 'BTC-USDT-SWAP'
    index = pd.date_range('2020-01-01', periods=501, tz='UTC')
    closes = 100 + np.sin(np.arange(501) / 5) * 5
    daily = pd.DataFrame(dict(open=closes, high=closes + 2, low=closes - 2, close=closes, volume=1000), index=index)
    t = index[-1] + pd.Timedelta(days=1)
    minute = pd.DataFrame(dict(open=[100.], high=[100.5], low=[99.5], close=[100.], volume=[1000.]), index=[t])
    funds = funding(t) if funding else pd.DataFrame(columns=['funding_rate', 'mark_price_at_event'], index=pd.DatetimeIndex([], tz='UTC'))
    return MinuteReplay({symbol: daily}, {symbol: minute}, {symbol: minute}, {symbol: funds},
                        [spec()], candidates()[0], start=t, end=t, scenario=scenario)


def test_minute_entry_and_cost_equity_reconcile():
    engine = replay()
    engine._orders = lambda t: [Order('BTC-USDT-SWAP', 'open', 1, 1., 'fixture', 'slow', 100, 2)]
    result = engine.run()
    opens = [e for e in result['events'] if e['action'] == 'OPEN']
    assert len(opens) == 1
    assert result['equity'].iloc[-1] == pytest.approx(100000 - opens[0]['fee'])
    assert opens[0]['leverage'] <= 3


def test_exact_funding_timestamp_selects_worse_cashflow_not_fixed_order():
    def funding(t):
        return pd.DataFrame(dict(funding_rate=[.01], mark_price_at_event=[100.]), index=[t])
    results = []
    for scenario in ('worst_case', 'best_case'):
        engine = replay(scenario, funding)
        engine._orders = lambda t: [Order('BTC-USDT-SWAP', 'open', 1, 1., 'fixture', 'slow', 100, 2)]
        results.append(engine.run())
    assert results[0]['equity'].iloc[-1] < results[1]['equity'].iloc[-1]
    assert results[0]['ambiguities'][0]['kind'] == 'FUNDING_ORDER_TIMESTAMP'


def test_minute_model_refuses_to_invent_millisecond_ordering():
    engine = replay()
    t = engine.start
    engine.end = t + pd.Timedelta(minutes=1)
    frame = engine.minutes['BTC-USDT-SWAP']
    frame.loc[t + pd.Timedelta(minutes=1)] = frame.iloc[0]
    engine.funding['BTC-USDT-SWAP'] = pd.DataFrame(dict(funding_rate=[.001], mark_price_at_event=[100.]), index=[t + pd.Timedelta(milliseconds=1)])
    with pytest.raises(ValueError, match='tick execution'):
        engine.run()


def test_daily_candidate_cannot_bypass_eligibility():
    engine = replay()
    with pytest.raises(ValueError, match='historical eligibility'):
        MinuteReplay(engine.daily, engine.minutes, engine.marks, engine.funding, [spec()],
                     dict(candidates()[0], architecture='daily'), start=engine.start, end=engine.end)


def test_historical_lower_leverage_respected():
    s = spec(exchange_max_leverage=2.)
    engine = replay()
    engine.spec_history = [s]
    engine._orders = lambda t: [Order('BTC-USDT-SWAP', 'open', 1, 1., 'fixture', 'slow', 100, 2)]
    assert next(e for e in engine.run()['events'] if e['action'] == 'OPEN')['leverage'] == 2


def test_missing_mark_minute_rejected():
    engine = replay()
    engine.marks = {'BTC-USDT-SWAP': engine.marks['BTC-USDT-SWAP'].iloc[:0]}
    with pytest.raises(ValueError, match='aligned'):
        engine.run()


def test_spec_conversion_on_open_position_is_explicit_failure():
    engine = replay()
    engine.active_specs['BTC-USDT-SWAP'] = replace(spec(), contract_value=2.)
    with pytest.raises(ValueError, match='conversion'):
        engine._spec('BTC-USDT-SWAP', engine.start)
