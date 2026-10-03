"""滚动验证与事件执行的独立反例；不修改旧Golden Master。"""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from inv_trend.core.执行约束 import ExecutionPolicy
from inv_trend.core.滚动协议 import make_windows,immutable_json,choose_from_training
from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.models.domain import AssetSpec,Order,TurtleRules,PortfolioState
from inv_trend.application.滚动验证 import prefix
from inv_trend.application.滚动评估 import summarize


def bars(n=100,offset=0):
    price=100+np.arange(n)*.25+np.sin(np.arange(n))
    index=pd.date_range('2020-01-01',periods=n,tz='UTC')
    return pd.DataFrame(dict(open=price,high=price+2,low=price-2,close=price+.1,volume=10000.,
        bar_open=index+pd.Timedelta(hours=offset),bar_end=index+pd.Timedelta(hours=offset+6)),index=index)


def engine(frame=None,policy=None,**kwargs):
    frame=bars() if frame is None else frame
    return TurtleBacktester({'A':frame},{'A':AssetSpec('A','metal','m',qty_step=1.)},
        initial_equity=100000.,liquidate_at_end=False,audit_execution=True,
        execution_policy=policy or ExecutionPolicy(),**kwargs)


def test_explicit_clock_requires_real_event_columns():
    with pytest.raises(ValueError,match='bar_open'):
        engine(bars().drop(columns=['bar_open','bar_end'])).run()


def test_full_future_prices_cannot_change_earlier_equity_or_orders():
    frame=bars(140)
    before=engine(frame.iloc[:100]).run()
    altered=frame.copy();altered.iloc[100:,:4]*=10
    after=engine(altered).run()
    pd.testing.assert_series_equal(before.equity_curve,after.equity_curve.loc[before.equity_curve.index],check_freq=False)
    # 最后收盘意图不影响同一前缀内的已成交订单。
    if len(before.orders):
        cols=['time','symbol','action','qty','fill_price','cost']
        right=after.orders.loc[pd.to_datetime(after.orders.time)<=before.equity_curve.index[-1],cols]
        pd.testing.assert_frame_equal(before.orders[cols].reset_index(drop=True),right.reset_index(drop=True))


def test_clock_processes_only_active_asset_no_later_session_lookahead():
    a,b=bars(3),bars(3,10)
    b.loc[b.index[1],['high','close']]=1000.
    specs={x:AssetSpec(x,'metal','m',qty_step=1.,cost_bps=0.,slippage_bps=0.) for x in ('A','B')}
    seen=[]
    class Strategy:
        def generate_orders(self,rows,state,equity,tradable_symbols):
            seen.append((set(tradable_symbols),{s:float(r['close']) for s,r in rows.items()}))
            return []
    runner=TurtleBacktester({'A':a,'B':b},specs,execution_policy=ExecutionPolicy(),
        liquidate_at_end=False,strategy=Strategy())
    runner.run()
    assert seen[0][0]=={'A'} and 'B' not in seen[0][1]
    assert seen[2][0]=={'A'} and seen[2][1]['B']<200


def test_lock_rejects_future_trained_schedule():
    with pytest.raises(ValueError,match='训练截止'):
        engine(policy_schedule=[dict(effective_at='2020-02-01',trained_through='2020-02-02',rules={})])


def test_train_prefix_uses_completion_not_session_label():
    f=bars(3,10)
    p=prefix({'A':f},pd.Timestamp('2020-01-02 12:00Z'))
    assert len(p['A'])==1


def test_walk_forward_has_gap_and_nonoverlapping_outer_windows():
    cfg=json.loads((Path(__file__).parents[1]/'config/滚动验证协议_v2.json').read_text())
    windows=make_windows(cfg['windows']);assert len(windows)==13
    for prev,nxt in zip(windows[:-1],windows[1:]):
        assert prev.test_end_exclusive==nxt.test_start
        assert pd.Timestamp(nxt.train_end)<pd.Timestamp(nxt.test_start)


def test_immutable_lock_cannot_be_replaced(tmp_path):
    path=tmp_path/'锁定.json';immutable_json(path,{'candidate':'A'})
    with pytest.raises(FileExistsError):immutable_json(path,{'candidate':'B'})
    assert json.loads(path.read_text())['candidate']=='A'


def test_selection_refuses_outer_metrics():
    with pytest.raises(ValueError,match='训练窗口'):
        choose_from_training({'A':{'scope':'OUTER'}},['A'],{})


def test_metric_initial_loss_cannot_disappear():
    eq=pd.Series([90.,95.],index=pd.date_range('2020-01-01',periods=2,tz='UTC'))
    m=summarize(eq,initial_equity=100.,start='2020-01-01')
    assert m['total_return']==pytest.approx(-.05)
    assert m['max_drawdown']==pytest.approx(.1)

@pytest.mark.parametrize('side',[1,-1])
def test_slippage_moves_price_and_is_not_double_debited(side):
    frame=bars(3)
    runner=engine(frame,ExecutionPolicy(price_slippage=True))
    runner.specs['A']=replace(runner.specs['A'],cost_bps=10,slippage_bps=20)
    state,orders,trades,details=PortfolioState(),[],[],[]
    order=Order('A','open',side,10.,'test','slow',100.,5.)
    day=frame.index[0]
    cash,_=runner._execute_orders(day,[order],100000.,state,orders,trades,details)
    entry=100.*(1+side*.002)
    assert orders[0]['fill_price']==pytest.approx(entry)
    assert orders[0]['cost']==pytest.approx(10*entry*.001)
    assert orders[0]['slippage_cost']==pytest.approx(2.)
    close=replace(order,action='exit',forced_fill_price=110.)
    cash,_=runner._execute_orders(frame.index[1],[close],cash,state,orders,trades,details)
    exit_=110.*(1-side*.002)
    net=side*10*(exit_-entry)-10*(entry+exit_)*.001
    assert cash-100000==pytest.approx(net)
    assert trades[0]['pnl']==pytest.approx(net)
    assert orders[1]['slippage_cost']==pytest.approx(2.2)


def test_gap_stop_uses_adverse_open_not_stale_stop():
    frame=bars(3);frame.iloc[1,:4]=[70.,73.,65.,71.]
    runner=engine(frame,ExecutionPolicy(price_slippage=True))
    state,orders,trades,details=PortfolioState(),[],[],[]
    order=Order('A','open',1,10.,'test','slow',100.,5.)
    cash,_=runner._execute_orders(frame.index[0],[order],100000.,state,orders,trades,details)
    runner._process_intraday_stops(frame.index[1],cash,state,orders,trades,details,open_only=True)
    assert orders[-1]['fill_price']==pytest.approx(70*(1-2/10000))
    assert not state.positions


def test_global_capital_budget_does_not_double_spend_for_two_assets():
    frame=bars(3)
    specs={x:AssetSpec(x,'metal','m',qty_step=1.,cost_bps=10,slippage_bps=20) for x in ['A','B']}
    runner=TurtleBacktester({'A':frame,'B':frame},specs,initial_equity=10000.,
        execution_policy=ExecutionPolicy(enforce_capital=True),liquidate_at_end=False)
    state,records,trades,details=PortfolioState(),[],[],[]
    orders=[Order(x,'open',1,100.,'test','slow',100.,5.) for x in ['A','B']]
    cash,_=runner._execute_orders(frame.index[0],orders,10000.,state,records,trades,details)
    prices={'A':100.,'B':100.}
    equity=runner._mark_equity_at_open(cash,state,prices)
    gross=sum(p.total_qty*100 for p in state.positions.values())
    assert gross<=equity and sum(x.total_qty for x in state.positions.values())<=99
    assert records[-1]['status']=='rejected'


def test_calendar_carry_charges_weekends_before_exit_and_reconciles():
    runner=engine(bars(4),ExecutionPolicy(charge_calendar_carry=True,financing_bps_per_year=365.2425))
    state,orders,trades,details=PortfolioState(),[],[],[]
    order=Order('A','open',1,10.,'test','slow',100.,5.)
    date=pd.Timestamp('2020-01-01',tz='UTC')
    cash,_=runner._execute_orders(date,[order],100000.,state,orders,trades,details)
    runner._known_prices={'A':100.}
    previous=cash
    cash=runner._accrue_calendar_cost(date+pd.Timedelta(days=3),cash,state)
    assert previous-cash==pytest.approx(.3)
    assert state.positions['A'].carry_cost==pytest.approx(.3)
    cash,_=runner._execute_orders(date+pd.Timedelta(days=3),[replace(order,action='exit')],cash,state,orders,trades,details)
    assert cash-100000==pytest.approx(trades[0]['pnl'])


def test_open_participation_uses_previous_volume_not_future_total():
    frame=bars(3);frame.loc[frame.index[0],'volume']=100.;frame.loc[frame.index[1],'volume']=1e10
    runner=engine(frame,ExecutionPolicy(enforce_volume=True))
    runner.specs['A']=replace(runner.specs['A'],asset_class='equity')
    state,records,trades,details=PortfolioState(),[],[],[]
    order=Order('A','open',1,100.,'test','slow',100.,5.)
    runner._execute_orders(frame.index[1],[order],100000.,state,records,trades,details)
    assert state.positions['A'].total_qty==1.


def test_no_borrow_permission_is_enforced_at_fill_even_for_custom_strategy():
    runner=engine();runner.specs['A']=replace(runner.specs['A'],can_short=False)
    state,records,trades,details=PortfolioState(),[],[],[]
    order=Order('A','open',-1,10.,'test','slow',100.,5.)
    runner._execute_orders(bars().index[1],[order],100000.,state,records,trades,details)
    assert not state.positions and records[-1]['status']=='rejected'


def test_per_asset_attribution_sums_exactly_to_account_equity_change():
    result=engine(bars(160)).run()
    grouped=result.attribution.groupby('time').total_pnl.sum()
    np.testing.assert_allclose(grouped.to_numpy(),result.equity_curve.to_numpy()-100000.,atol=1e-8)


def test_same_runner_repeated_run_is_identical_including_all_ledgers():
    runner=engine(bars(140))
    a,b=runner.run(),runner.run()
    pd.testing.assert_series_equal(a.equity_curve,b.equity_curve)
    pd.testing.assert_frame_equal(a.attribution,b.attribution)
    pd.testing.assert_frame_equal(a.cash_ledger,b.cash_ledger)
    pd.testing.assert_frame_equal(a.orders,b.orders)


def test_bar_overlap_and_naive_timezone_fail_closed():
    frame=bars(5);frame.loc[frame.index[1],'bar_open']=frame.bar_open.iloc[0]
    with pytest.raises(ValueError,match='重叠'):engine(frame).run()
    frame=bars(5);frame['bar_open']=frame.bar_open.dt.tz_localize(None)
    with pytest.raises(ValueError,match='时区'):engine(frame).run()


def test_all_prior_history_retained_so_wilder_seed_is_not_window_dependent():
    data={'A':bars(600)}
    a=prefix(data,data['A'].bar_end.iloc[-1],start=data['A'].index[400])
    b=prefix(data,data['A'].bar_end.iloc[-1],start=data['A'].index[500])
    pd.testing.assert_frame_equal(a['A'],b['A'])


def test_mutating_outer_window_cannot_change_training_objects():
    data={'A':bars(400)};cutoff=data['A'].bar_end.iloc[200]
    a=prefix(data,cutoff)
    data['A'].iloc[201:,:4]*=10
    b=prefix(data,cutoff)
    pd.testing.assert_frame_equal(a['A'],b['A'])


def test_corrected_bootstrap_is_deterministic_and_identical_candidates_cannot_upgrade():
    from inv_trend.application.滚动统计 import family_bootstrap
    returns=np.random.default_rng(8).normal(.0001,.01,300)
    frame=pd.DataFrame({'A':returns,'B':returns})
    a=family_bootstrap(frame,repetitions=100,seed=42)
    b=family_bootstrap(frame,repetitions=100,seed=42)
    assert a==b
    assert a['results'][1]['adjusted_p_vs_baseline']==1.


def test_inspected_history_cannot_pass_promotion_even_with_good_returns():
    from inv_trend.application.滚动统计 import promotion_decision
    cfg=json.loads((Path(__file__).parents[1]/'config/滚动验证协议_v2.json').read_text())
    metric=dict(total_return=1.,max_drawdown=.1,sharpe_ratio=2.,calmar_ratio=2.,positive_fold_ratio=1.)
    decision=promotion_decision(cfg,{'metrics':metric},{'metrics':metric},
        {'adjusted_p_vs_baseline':.001},[{'metrics':metric}])
    assert not decision['promoted'] and not decision['production_enabled']
    assert not decision['gates']['确有未见前瞻数据']


def test_registry_reopens_explicit_root_without_creating_second_authority(tmp_path):
    from inv_trend.application.perpetual_audit.研究协议 import ExperimentRegistry
    root=tmp_path/'项目';path=tmp_path/'外部审计'/'登记.jsonl'
    registry=ExperimentRegistry(path,root=root)
    rid=registry.start({},code_hash='x',data_hash='y',sample_status='DEVELOPMENT')
    registry.finish(rid,'COMPLETED')
    reopened=ExperimentRegistry(path)
    assert len(reopened.read())==2 and reopened.backend.store.root==root


def test_registry_sqlite_tamper_is_not_accepted(tmp_path):
    from inv_trend.application.perpetual_audit.研究协议 import ExperimentRegistry
    registry=ExperimentRegistry(tmp_path/'登记.jsonl')
    registry.start({},code_hash='x',data_hash='y',sample_status='DEVELOPMENT')
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        registry.backend.store.tx(lambda db:db.execute("UPDATE experiment_events SET payload_json=replace(payload_json,'DEVELOPMENT','OTHER')"))
    # 故障注入：模拟有数据库权限的外部损坏，绕过SQL触发器后仍须被哈希链拒绝。
    def corrupt(db):
        names=[x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='experiment_events'")]
        for name in names:db.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
        db.execute("UPDATE experiment_events SET payload_json=replace(payload_json,'DEVELOPMENT','OTHER')")
    registry.backend.store.tx(corrupt)
    with pytest.raises(ValueError,match='integrity'):registry.read()


def test_non_positive_terminal_equity_has_no_fabricated_cagr():
    eq=pd.Series([100.,-10.],index=pd.date_range('2020-01-01',periods=2,tz='UTC'))
    result=summarize(eq,initial_equity=100.)
    assert result['annualized_return'] is None and result['calmar_ratio'] is None
    assert result['bankrupt'] and result['total_return']==pytest.approx(-1.1)


def test_strict_execution_rejects_non_positive_stop():
    runner=engine(bars(3));state=PortfolioState();records=[]
    order=Order('A','open',1,10.,'test','slow',100.,100.)
    cash,_=runner._execute_orders(bars().index[0],[order],100000.,state,records,[],[])
    assert not state.positions and cash==100000.
    assert records[0]['resolution']=='non-positive protective stop'


def test_schedule_activation_waits_for_open_after_old_close():
    runner=engine(bars(140))
    times=[]
    runner.policy_schedule=[dict(effective_at='2020-03-01T00:00:00Z',trained_through='2020-02-20T00:00:00Z',rules={})]
    original=runner._apply_scheduled_policy
    def tracked(*args):
        times.append(runner._event_phase)
        return original(*args)
    runner._apply_scheduled_policy=tracked
    runner.run()
    assert times and set(times)=={'open'}


def test_cancelled_reservation_keeps_identity_without_fees():
    from inv_trend.adapters.multi_asset.models.order_intent import PendingOrderIntent, ReservationBook
    runner=engine();book=ReservationBook();records=[]
    order=Order('A','open',1,10.,'test','slow',100.,5.)
    intent=PendingOrderIntent(order=order,created_at='2020-01-01T20:00:00Z',
        signal_bar_time='2020-01-01T00:00:00Z',entry_period=20,breakout_level=100.,
        requested_qty=10.,reserved_risk=.01,reserved_notional=.1,eligible_from=None,intent_id='固定身份')
    book.reserve(intent)
    runner._cancel_intents(pd.Timestamp('2020-01-02',tz='UTC'),book,records,'walk_forward_policy_replaced')
    assert not book.intents
    assert records[0]['intent_id']=='固定身份' and records[0]['status']=='cancelled'
    assert records[0]['fee_cost']==records[0]['slippage_cost']==0.


def test_repeated_scheduled_execution_preserves_continuous_cash_and_orders():
    data=bars(220)
    schedule=[dict(effective_at='2020-01-01T00:00:00Z',trained_through='2019-12-20T00:00:00Z',rules={'stop_n':2.}),
        dict(effective_at='2020-06-01T00:00:00Z',trained_through='2020-05-20T00:00:00Z',rules={'stop_n':3.})]
    runner=engine(data,policy_schedule=schedule)
    a,b=runner.run(),runner.run()
    pd.testing.assert_series_equal(a.equity_curve,b.equity_curve)
    pd.testing.assert_frame_equal(a.cash_ledger,b.cash_ledger)
    pd.testing.assert_frame_equal(a.orders,b.orders)
    # 每个事件的品种损益累计与同一初始本金对账，不能在窗口边界重置资金。
    total=a.attribution.groupby('time').total_pnl.sum()
    np.testing.assert_allclose(total.to_numpy(),a.equity_curve.to_numpy()-100000.,atol=1e-8)


def test_future_mutation_cannot_change_training_scores_or_selected_parameters(tmp_path):
    from inv_trend.application.滚动验证 import train_candidates
    from inv_trend.core.滚动协议 import WalkForwardWindow, digest
    cfg=json.loads((Path(__file__).parents[1]/'config/滚动验证协议_v2.json').read_text())
    cfg['specs']={'A':dict(cfg['specs']['XAUUSD_DUKAS'])}
    data={'A':bars(1450)}
    window=WalkForwardWindow('窗口测试','2020-01-01T00:00:00Z','2023-01-01T00:00:00Z',
        '2023-01-08T00:00:00Z','2023-07-08T00:00:00Z')
    first=train_candidates(data,cfg,window,cfg['candidates'][:2],tmp_path/'原始')
    changed={'A':data['A'].copy()}
    changed['A'].loc[changed['A'].index>=pd.Timestamp(window.train_end),['open','high','low','close']]*=1000.
    second=train_candidates(changed,cfg,window,cfg['candidates'][:2],tmp_path/'未来污染')
    assert digest(first)==digest(second)
    ids=[c['id'] for c in cfg['candidates'][:2]]
    assert choose_from_training(first,ids,cfg['selection'])==choose_from_training(second,ids,cfg['selection'])


def test_saved_run_verifier_detects_tampered_bytes(tmp_path):
    from inv_trend.storage.滚动核验 import verify_research
    from inv_trend.application.滚动验证 import write_run
    immutable_json(tmp_path/'实验协议冻结.json',{'protocol':{'dataset_sha256':'数据版本'},'source_sha256':'代码版本'})
    immutable_json(tmp_path/'完整迭代摘要.json',[])
    write_run(tmp_path/'运行',engine(bars(80)).run(),{'data_version':'数据版本'})
    assert verify_research(tmp_path)['runs']==1
    (tmp_path/'运行/权益曲线.csv.gz').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='校验失败'):verify_research(tmp_path)
