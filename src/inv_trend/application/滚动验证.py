"""预注册的嵌套滚动验证：参数选择只接收训练前缀，外层只用于审计。

一个执行器、一个资金账户。窗口换参时先在下一真实开盘平仓，不能拼接重置本金的曲线。
"""
from __future__ import annotations
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
import pandas as pd

from inv_trend.core.执行约束 import ExecutionPolicy
from inv_trend.core.滚动协议 import make_windows, choose_from_training, digest, immutable_json
from inv_trend.adapters.multi_asset.data.cleaner import clean_ohlcv_frame
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, TurtleRules
from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.application.滚动评估 import summarize, daily_returns


def load_data(root, config):
    path = Path(root) / config['dataset']
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != config['dataset_sha256']:
        raise ValueError('行情文件SHA256与协议不一致，必须新建数据版本而不是复用实验')
    raw = pd.read_csv(path, float_precision='round_trip')
    raw['date'] = pd.to_datetime(raw['date'], utc=True, errors='raise')
    clean = clean_ohlcv_frame(raw, duplicate_policy='error')
    if len(clean) != len(raw):
        raise ValueError('输入含需要删除的异常/重复行，必须先独立发布清洗版本')
    out = {}
    for symbol, part in clean.groupby('symbol', sort=True):
        if symbol not in config['specs']:
            raise ValueError(f'{symbol}: 缺少明确资产合同')
        frame = part.set_index('date').drop(columns=['symbol']).sort_index()
        dates = frame.index.tz_localize(None).normalize()
        if config['specs'][symbol]['asset_class'] == 'equity':
            # 来源未认证，仅常规交易时段假设；没有假装认证半日市/假日或复权。
            frame['bar_open'] = (dates + pd.Timedelta(hours=9, minutes=30)).tz_localize('America/New_York').tz_convert('UTC')
            frame['bar_end'] = (dates + pd.Timedelta(hours=16)).tz_localize('America/New_York').tz_convert('UTC')
        else:
            frame['bar_open'] = frame.index
            frame['bar_end'] = frame.index + pd.Timedelta(days=1)
        out[symbol] = frame
    return out


def prefix(data, cutoff, start=None, warmup=None):
    cutoff = pd.Timestamp(cutoff)
    out = {}
    for symbol, frame in data.items():
        # 物理裁剪后的对象才交给训练函数；截断点以真实可用的收盘时间为准。
        part = frame.loc[pd.to_datetime(frame.bar_end, utc=True) < cutoff]
        if start is not None and warmup is not None:
            before = part.loc[part.index < pd.Timestamp(start)].tail(warmup)
            part = pd.concat([before, part.loc[part.index >= pd.Timestamp(start)]])
        if not part.empty:
            out[symbol] = part.copy()
    if not out:
        raise ValueError('所需前缀没有已完成K线')
    return out


def specs_for(data, config, candidate, cost_multiple=1.):
    specs = {}
    for symbol in data:
        params = dict(config['specs'][symbol])
        params.update(candidate.get('spec_overrides', {}))
        # 股票无点时借券清单，默认禁空；金属仅研究合成合约，不获实盘认证。
        params['can_short'] = params['asset_class'] != 'equity'
        params['max_symbol_leverage'] = .35
        params['cost_bps'] *= cost_multiple
        params['slippage_bps'] *= cost_multiple
        specs[symbol] = AssetSpec(symbol=symbol, **params)
    return specs


def cash_candidate(base):
    return {**base, 'id':'现金','name':'训练门槛未通过，保持现金',
            'rules':{**base['rules'],'fast_system_enabled':False,'slow_system_enabled':False},
            'take_profit_n':0.,'spec_overrides':{}}


def simulate(data, config, candidate, start, end, *, schedule=None, cost_multiple=1., delay=1):
    scoped = prefix(data, end, start, config['windows']['warmup_bars'])
    specs = specs_for(scoped, config, candidate, cost_multiple)
    rules = TurtleRules(**candidate['rules'])
    execution = ExecutionPolicy(**config['execution'])
    started = time.perf_counter()
    runner = TurtleBacktester(scoped, specs, rules, initial_equity=100000., cash_model='derivative',
        liquidate_at_end=False, evaluation_start=start,
        evaluation_end=pd.Timestamp(end)-pd.Timedelta(nanoseconds=1),
        take_profit_n=candidate.get('take_profit_n',0.),signal_delay_bars=delay,
        audit_execution=True, execution_policy=execution, policy_schedule=schedule)
    result = runner.run()
    metrics = summarize(result.equity_curve, result.trades, result.orders,
        start=start, carry_cost=result.carry_cost, open_positions=result.open_position_count)
    return result, metrics, time.perf_counter()-started


def write_run(folder, result, metadata):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    files = {}
    frames = {'权益曲线':result.equity_curve, '交易记录':result.trades, '订单记录':result.orders,
              '分笔明细':result.trade_details, '资金流水':result.cash_ledger, '策略证据':result.decisions, '品种归因':result.attribution}
    for name, frame in frames.items():
        path = folder/(name+'.csv.gz')
        frame.to_csv(path, index=isinstance(frame,pd.Series),compression={'method':'gzip','mtime':0})
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = {**metadata,'files_sha256':files,'created_at':datetime.now(timezone.utc).isoformat()}
    immutable_json(folder/'运行记录.json',metadata)
    return metadata


def train_candidates(data, config, window, candidates, output):
    """不接收外层对象。三段训练内回放全部在train_end之前，窗口间没有标注泄漏。"""
    train_data = prefix(data, window.train_end)
    assert max(pd.to_datetime(f.bar_end).max() for f in train_data.values()) < pd.Timestamp(window.train_end)
    ends = pd.date_range(pd.Timestamp(window.train_start),pd.Timestamp(window.train_end),
                         periods=config['windows']['inner_slices']+1)
    records = {}
    for candidate in candidates:
        slices = []
        for i, (start,end) in enumerate(zip(ends[:-1],ends[1:]),1):
            # 日线的内层分割边界取UTC午夜，固定规则而非收益择时。
            start,end = start.normalize(),end.normalize()
            result,metric,seconds = simulate(train_data,config,candidate,start,end)
            meta = dict(scope='TRAIN_ONLY',candidate_id=candidate['id'],parameters=candidate,
                window_id=window.window_id,train_start=start.isoformat(),train_end=end.isoformat(),
                data_version=config['dataset_sha256'],metrics=metric,seconds=seconds)
            write_run(Path(output)/window.window_id/candidate['id']/f'内层{i:02d}',result,meta)
            slices.append(metric)
        records[candidate['id']] = dict(scope='TRAIN_ONLY',train_start=window.train_start,
            train_end=window.train_end,slices=slices)
    return records


def fold_metrics(result, windows):
    rows = []
    eq = result.equity_curve
    for window in windows:
        start,end = pd.Timestamp(window.test_start),pd.Timestamp(window.test_end_exclusive)
        selected = eq[(eq.index>=start)&(eq.index<end)]
        if selected.empty:
            continue
        before = eq[eq.index<start]
        initial = float(before.iloc[-1]) if len(before) else 100000.
        tr = result.trades
        od = result.orders
        if len(tr): tr = tr[(pd.to_datetime(tr.exit_time)>=start)&(pd.to_datetime(tr.exit_time)<end)]
        if len(od): od = od[(pd.to_datetime(od.time)>=start)&(pd.to_datetime(od.time)<end)]
        ledger = result.cash_ledger
        curr=ledger.loc[(pd.to_datetime(ledger.time)>=start)&(pd.to_datetime(ledger.time)<end)]
        prev=ledger.loc[pd.to_datetime(ledger.time)<start]
        carry = float(curr.carry_cost_cumulative.iloc[-1]) - (float(prev.carry_cost_cumulative.iloc[-1]) if len(prev) else 0.)
        met=summarize(selected,tr,od,initial_equity=initial,start=start,carry_cost=carry,
                      open_positions=int(curr.open_positions.iloc[-1]))
        rows.append({**window.as_dict(),'initial_equity':initial,'metrics':met})
    return rows


def run_research(root, output, *, iterations=None, candidate_limit=None, execution_override=None,
                 framework_label=None, skip_stress=False):
    root,output = Path(root),Path(output)
    config=json.loads((root/'config/滚动验证协议_v2.json').read_text(encoding='utf-8'))
    if execution_override:
        config['execution'].update(execution_override)
    candidates=config['candidates'][:candidate_limit]
    allowed_ids={x['id'] for x in candidates}
    steps=[x for x in config['iterations'] if set(x['pool']).issubset(allowed_ids)]
    if iterations is not None:steps=steps[:iterations]
    if output.exists(): raise FileExistsError('输出目录已存在，拒绝覆盖或混合旧实验')
    output.mkdir(parents=True)
    code_hash=source_digest(root)
    identity=dict(protocol=config,source_sha256=code_hash,python=platform.python_version(),
                  numpy=np.__version__,pandas=pd.__version__,framework_label=framework_label)
    immutable_json(output/'实验协议冻结.json',identity)
    data=load_data(root,config)
    from inv_trend.application.数据认证 import environment_status, assess_data
    immutable_json(output/'环境核验.json',environment_status())
    immutable_json(output/'数据审计.json',assess_data(data,config))
    windows=make_windows(config['windows'])
    immutable_json(output/'窗口划分.json',[x.as_dict() for x in windows])
    training={}
    for window in windows:
        training[window.window_id]=train_candidates(data,config,window,candidates,output/'训练证据')
        print(json.dumps({'阶段':'训练完成','窗口':window.window_id,'候选':len(candidates)},ensure_ascii=False),flush=True)
    immutable_json(output/'完整训练摘要.json',training)
    results=[]
    cash=cash_candidate(candidates[0]);candidate_map={x['id']:x for x in candidates};candidate_map['现金']=cash
    for step in steps:
        locks=[];schedule=[]
        for window in windows:
            # 第一轮为此前冻结v4固定策略对照；后续轮次使用严格训练门槛。
            selected,ranks=choose_from_training(training[window.window_id],step['pool'],config['selection'])
            if len(step['pool']) == 1: selected=candidates[0]['id']
            c=candidate_map[selected]
            entry=dict(effective_at=window.test_start,trained_through=window.train_end,
                       rules=c['rules'],candidate_id=c['id'],take_profit_n=c['take_profit_n'],
                       specs=specs_for(data,config,c))
            schedule.append(entry)
            lock=dict(iteration=step['id'],**window.as_dict(),candidate_id=selected,
                parameters=c,ranking=ranks,train_records_sha256=digest(training[window.window_id]),
                selection_inputs='仅TRAIN_ONLY摘要',outer_metrics_present=False,
                created_at=datetime.now(timezone.utc).isoformat())
            # 冻结文件先于任何本轮外层执行；每个选择仅绑定该窗口可见训练前缀。
            immutable_json(output/'参数锁定'/step['id']/(window.window_id+'.json'),lock)
            locks.append(lock)
        result,metrics,seconds=simulate(data,config,candidates[0],windows[0].test_start,
            windows[-1].test_end_exclusive,schedule=schedule)
        folds=fold_metrics(result,windows)
        metrics['positive_fold_ratio']=float(np.mean([x['metrics']['total_return']>0 for x in folds]))
        metrics['selection_change_ratio']=float(np.mean([a['candidate_id']!=b['candidate_id'] for a,b in zip(locks[:-1],locks[1:])]))
        metrics['cash_window_count']=sum(x['candidate_id']=='现金' for x in locks)
        previous=results[-1]['metrics'] if results else metrics
        original=results[0]['metrics'] if results else metrics
        delta=lambda ref:{k:metrics[k]-ref[k] for k in ['total_return','annualized_return','max_drawdown','sharpe_ratio','calmar_ratio','trade_count']
                          if metrics[k] is not None and ref[k] is not None}
        record=dict(iteration=step['id'],strategy_version=config['strategy_version'],change=step['change'],
            pool=step['pool'],metrics=metrics,folds=folds,locks=locks,seconds=seconds,
            delta_previous=delta(previous),delta_original=delta(original),data_version=config['dataset_sha256'],
            status='研究诊断，未升级',source_sha256=code_hash,
            analysis={'procedure':'按预先冻结的候选池扩展；外层结果不反馈下一轮参数设计',
                      'negative_fold_count':sum(x['metrics']['total_return']<0 for x in folds),
                      'return_below_previous':metrics['total_return']<previous['total_return'],
                      'drawdown_above_previous':metrics['max_drawdown']>previous['max_drawdown'],
                      'next_action':'维持预注册下一轮；下降结果同样保留；不能因外层收益升级'})
        write_run(output/'样本外证据'/step['id'],result,record)
        results.append(record)
        immutable_json(output/'迭代摘要'/(step['id']+'.json'),record)
        print(json.dumps({'阶段':step['id'],'收益':metrics['total_return'],'回撤':metrics['max_drawdown'],
                          'Sharpe':metrics['sharpe_ratio'],'窗口数':len(folds)},ensure_ascii=False),flush=True)
    immutable_json(output/'完整迭代摘要.json',results)
    stress=[]
    if not skip_stress:
        # 压力诊断仅对预先指定的最后研究轮次，不按样本外收益选择压力对象。
        stress=[]
        for scenario in config['stress']:
            stressed_schedule=[]
            for entry in schedule:
                c=candidate_map[entry['candidate_id']]
                stressed_schedule.append({**entry,'specs':specs_for(data,config,c,scenario['cost_multiple'])})
            rr,mm,ss=simulate(data,config,candidates[0],windows[0].test_start,windows[-1].test_end_exclusive,
                schedule=stressed_schedule,cost_multiple=scenario['cost_multiple'],delay=scenario['delay'])
            record=dict(scenario=scenario,metrics=mm,seconds=ss,folds=fold_metrics(rr,windows),
                        policy_retrained=False,selection_locks_reused=True)
            write_run(output/'压力证据'/scenario['name'],rr,record);stress.append(record)
        immutable_json(output/'压力摘要.json',stress)
    from inv_trend.application.滚动统计 import family_bootstrap, promotion_decision
    returns={}
    for record in results:
        eq=pd.read_csv(output/'样本外证据'/record['iteration']/'权益曲线.csv.gz',index_col=0,parse_dates=True).iloc[:,0]
        returns[record['iteration']]=daily_returns(eq)
    params=config['statistical_audit']
    statistics=family_bootstrap(returns,repetitions=params['bootstrap_replicates'],
        block_length=params['block_calendar_days'],seed=params['seed'])
    immutable_json(output/'多重比较审计.json',statistics)
    decision=promotion_decision(config,results[0],results[-1],statistics['results'][-1],stress)
    immutable_json(output/'发布门禁.json',decision)
    return results


def source_digest(root):
    root=Path(root)
    h=hashlib.sha256()
    for path in sorted((root/'src').rglob('*.py')):
        h.update(str(path.relative_to(root)).encode());h.update(path.read_bytes())
    return h.hexdigest()
