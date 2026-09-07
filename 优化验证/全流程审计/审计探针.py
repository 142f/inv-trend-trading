"""只读生产代码的可复跑审计探针；输出仅限本目录。"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
import numpy as np
import pandas as pd
from inv_trend.core.features import FeatureRequest, PreparedBars, FeatureCache
from inv_trend.core.math_utils import directional_movement_index, wilder_average
from inv_trend.core.resampling import aggregate_completed_sessions
from inv_trend.data.processing import normalize_bars, apply_equity_adjustments, resample_ohlcv_session
from inv_trend.data.calendar import classify_missing
from inv_trend.data.config.loader import load_instruments
from inv_trend.data.api import HistoricalDataService
from inv_trend.adapters.multi_asset.backtest.data_store import BacktestDataStore
from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, TurtleRules, PortfolioState, Order
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.application.backtest.projector import StrategyReplayProjector, resolve_specs
from inv_trend.application.backtest.source import load_versioned_data, load_source_bundle
from tests.unit.test_strategy_backtest_stage import _source, _plan, _bars

RESULTS = []
def probe(name):
    def decorate(fn):
        start = time.perf_counter()
        try:
            value = fn()
            RESULTS.append(dict(id=name, status='completed', seconds=time.perf_counter()-start, **value))
        except Exception as exc:
            RESULTS.append(dict(id=name, status='probe_error', exception=type(exc).__name__, message=str(exc)))
        return fn
    return decorate

def bars(n=100):
    c = 100. + np.arange(n) * 1.0
    return pd.DataFrame(dict(open=c-.1, high=c+.2, low=c-.2, close=c, volume=1000.),
                        index=pd.date_range('2024-01-01', periods=n, tz='UTC'))

def files_hash():
    files = sorted(p for directory in ('src', 'config', 'tests') for p in (ROOT/directory).rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.yaml','.json','.css','.js','.sql'))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

before = files_hash()

@probe('P01_指标前缀与未来价格扰动')
def _():
    x = _bars(420); x.attrs = {}
    request = FeatureRequest(atr_period=14, donchian_periods=(10,20,55),
        sma_lags=((5,0),(20,1),(120,0)), ema_periods=(144,169), macd_periods=(12,26,9),
        dmi_period=14, volume_sma_lags=((20,1),))
    full = PreparedBars.build(x, request).frame
    points = [1,13,14,20,27,55,120,200,300,419]
    errors=[]
    for n in points:
        prefix = PreparedBars.build(x.iloc[:n], request).frame
        try: pd.testing.assert_frame_equal(full.iloc[:n], prefix, check_exact=False, rtol=1e-12, atol=1e-12)
        except AssertionError as e: errors.append(dict(n=n,error=str(e)[:200]))
    mutated=x.copy(); mutated.loc[mutated.index[300:], ['open','high','low','close']] *= 10
    changed=PreparedBars.build(mutated,request).frame
    pd.testing.assert_frame_equal(full.iloc[:300], changed.iloc[:300])
    return dict(prefix_points=points, errors=errors, future_price_mutation_preserves_prefix=True)

@probe('P02_尾部存在性改变交易资格与信号')
def _():
    x=bars(80); y=bars(100)
    rules=TurtleRules(n_period=2,fast_entry=3,slow_entry=5,fast_exit=2,slow_exit=3,skip_fast_after_win=False)
    specs={s:AssetSpec(s,'crypto','crypto') for s in ('TEST','OTHER')}
    output=[]
    for frame in (x,bars(81)):
        store=BacktestDataStore({'TEST':frame,'OTHER':y},rules)
        for t, rows, active in store.timeline():
            if t==x.index[-1]:
                orders=MultiAssetTurtleStrategy(specs,rules).generate_orders(rows,PortfolioState(),100000.,active)
                output.append(dict(at=str(t),active=sorted(active), test_orders=[o.action for o in orders if o.symbol=='TEST']))
                break
    return dict(without_future_bar=output[0],with_future_bar=output[1],history_through_T_identical=True)

@probe('P03_访问边界不存在')
def _():
    x=bars(80); store=BacktestDataStore({'TEST':x},TurtleRules())
    first=next(store.timeline())[0]
    return dict(current=str(first),future_date=str(x.index[-1]),
        unrestricted_future_close=store.row_at_date('TEST',x.index[-1])['close'],
        current_bar_close_exposed_in_open_reader=store.price(first,'TEST','close'))

@probe('P04_未知数据末尾触发提前清仓')
def _():
    frames=[]
    for n in (80,81):
        engine=TurtleBacktester({'TEST':bars(n),'OTHER':bars(100)},
            {s:AssetSpec(s,'crypto','crypto') for s in ('TEST','OTHER')},initial_equity=100000.,liquidate_at_end=True)
        class Strategy:
            def generate_orders_for_date(self,date,rows,state,equity,tradable_symbols):
                return [Order('TEST','open',1,1.,'audit','slow',100.,10.)] if date==bars(1).index[0] else []
        engine.strategy=Strategy(); result=engine.run()
        frames.append(result.trades.to_dict('records'))
    return dict(no_future_bar=frames[0],appended_one_bar=frames[1],explicit_evaluation_end=None)

@probe('P05_不完整Bar与跨周期边界')
def _():
    x=bars(10); x['is_complete']=True; x.loc[x.index[-1],'is_complete']=False
    prepared=PreparedBars.build(x,FeatureRequest(atr_period=2))
    store=BacktestDataStore({'TEST':x},TurtleRules())
    aggregated=aggregate_completed_sessions(x,2,anchor=x.index[0])
    return dict(feature_rows=len(prepared.frame),store_rows=len(store.by_symbol['TEST'].bars),
                aggregate_rows=len(aggregated),aggregate_last=str(aggregated.index[-1]),
                incomplete_last=str(x.index[-1]), scope='直接公开内存入口；版本化加载器另有完成过滤')

@probe('P06_小时日历虚报缺失')
def _():
    stamps=pd.date_range('2024-01-01',periods=24,freq='h',tz='UTC')
    absent, breakdown=classify_missing(stamps,market='binance',session='24x7',start=stamps[0],end=stamps[-1],freq='1h')
    return dict(actual_missing=0,reported_missing=len(absent),breakdown=breakdown)

@probe('P07_无穷值与字符串完成标记')
def _():
    instrument=load_instruments()['BTC']
    raw=bars(2).reset_index(names='timestamp'); raw.loc[0,['open','high']]=np.inf
    raw['is_complete']='false'
    r=normalize_bars(raw,instrument,'D1','audit',now=datetime(2025,1,1,tzinfo=timezone.utc))
    return dict(clean_rows=len(r.clean),quarantine_rows=len(r.quarantine),
        infinite_open_survives=bool(np.isinf(r.clean['open']).any()),
        string_false_completed=r.clean['is_complete'].tolist())

@probe('P08_复权辅助函数缺省列崩溃')
def _():
    x=bars(2).reset_index(names='timestamp')
    try: apply_equity_adjustments(x)
    except Exception as e: return dict(observed_exception=type(e).__name__,message=str(e),scope='孤立辅助函数；源码无生产调用')
    return dict(observed_exception=None)

@probe('P09_同数据日常与回测复权不一致')
def _():
    from inv_trend.application.backtest import source as source_module
    instrument=next(v for v in load_instruments().values() if v.asset_class=='equity')
    raw=bars(2).reset_index(names='timestamp')
    raw['adjusted_close']=raw['close']/2; raw['is_complete']=True
    raw['quality_status']='CURATED'; raw['quality_score']=100; raw['data_source']='audit'; raw['dataset_version']='version-1'
    daily=HistoricalDataService._strategy_visible_bars(raw.copy(),symbol='TEST',timeframe='D1',instrument=instrument,
        start=None,end=None,adjusted=True,completed_only=True,allow_research=False,allow_legacy=False,min_quality_score=50,quality_report='audit')
    verified=SimpleNamespace(curated_path=OUT/'synthetic.parquet',dataset_manifest_path=OUT/'synthetic.json',dataset_manifest={})
    with patch.object(source_module,'DataLake'), patch.object(source_module,'load_versioned_lineage',return_value=verified), patch.object(source_module.pd,'read_parquet',side_effect=lambda p:raw.copy()):
        backtest,_=load_versioned_data(_source(),OUT)
    return dict(daily_close=daily.close.tolist(),backtest_close=backtest['TEST'].close.tolist(),
        scope='真实转换函数；仅磁盘与血缘I/O使用同一合成帧替身，不证明现有历史交易受影响')

@probe('P10_缓存可变对象污染')
def _():
    x=bars(80); request=FeatureRequest(atr_period=14); cache=FeatureCache()
    a=cache.prepare(x,request); original=float(a.frame.atr.iloc[-1]); a.frame.loc[x.index[-1],'atr']=999.
    b=cache.prepare(x,request)
    return dict(original=original,reused_value=float(b.frame.atr.iloc[-1]),same_object=a is b)

@probe('P11_投影缓存未绑定实际数据')
def _():
    p=StrategyReplayProjector(_source()); x=_bars(100)
    _,a,_=p.project({'TEST':x},{})
    y=x.copy(); y.loc[y.index[-1],['open','high','low','close']]*=2
    _,b,_=p.project({'TEST':y},{})
    return dict(same_store=a is b,provided_last_close=float(y.close.iloc[-1]),cached_last_close=float(b.by_symbol['TEST'].bars.close.iloc[-1]),
                scope='同一 projector 被复用且传入变更数据；常规批次只传固定数据')

@probe('P12_共享计算与缓存计数')
def _():
    import inv_trend.core.features as f
    import inv_trend.core.math_utils as m
    request=FeatureRequest(atr_period=14,dmi_period=14,ema_periods=(12,26),macd_periods=(12,26,9))
    x=bars(3000)
    with patch.object(f,'true_range',wraps=f.true_range) as tr1, patch.object(m,'true_range',wraps=m.true_range) as tr2, patch.object(f,'exponential_moving_average',wraps=f.exponential_moving_average) as ema:
        PreparedBars.build(x,request)
        counts=dict(true_range=tr1.call_count+tr2.call_count,ema_in_feature_layer=ema.call_count)
    measures=[]
    for attrs in (False,True):
        frame=x.copy()
        if attrs: frame.attrs['dataset_version']='audit'
        cache=FeatureCache(); tracemalloc.start(); start=time.perf_counter()
        with patch.object(f,'ohlcv_fingerprint',wraps=f.ohlcv_fingerprint) as fp, patch.object(f.PreparedBars,'_build_validated',wraps=f.PreparedBars._build_validated) as build:
            a=cache.prepare(frame,request); b=cache.prepare(frame,request)
            measure=dict(attrs=attrs,fingerprints=fp.call_count,feature_builds=build.call_count,same_object=a is b)
        measure.update(seconds=time.perf_counter()-start,peak_python_bytes=tracemalloc.get_traced_memory()[1]); tracemalloc.stop(); measures.append(measure)
    return dict(rows=3000,primitive_counts=counts,cache_measurements=measures,measurement_scope='单次本机微基准，tracemalloc不代表进程RSS')

@probe('P13_行情冲突含缺失值误判一致')
def _():
    raw=pd.concat([bars(1).reset_index(names='timestamp')]*2,ignore_index=True)
    raw.loc[1,'volume']=np.nan
    r=normalize_bars(raw,load_instruments()['BTC'],'D1','audit')
    return dict(conflicting_duplicates=r.conflicting_duplicate_count,quarantine_reasons=r.quarantine.quarantine_reason.tolist())

@probe('P14_版本化来源未核验旧制品内容哈希')
def _():
    folder=OUT/'合成旧制品'/'TEST'/'01_canonical'; folder.mkdir(parents=True,exist_ok=True)
    for name in ('data_update','strategy_screening','trend_decision'):
        (folder/f'{name}_result.json').write_text(json.dumps(dict(symbol='TEST',timeframe='D1',dataset_version='v-test',result_hash='z'*64,decision='TAMPERED')),encoding='utf-8')
    source=load_source_bundle(folder.parents[1],_plan())
    return dict(accepted_non_hex_unverified_hash=source.instruments[0].decision_result_hash,scope='旧01_canonical路径；v3路径另有manifest校验')

@probe('P15_现金模型允许无借贷证据做空')
def _():
    from tests.test_执行真实性审计 import engine, order
    runner=engine(cash_model='cash'); state=PortfolioState(); records=[]
    cash,_=runner._execute_orders(runner.market_data.calendar[0],[order(side=-1)],10000.,state,records,[],[])
    source=_source(); specs=resolve_specs(source,{})
    return dict(short_position_created=state.positions['TEST'].side,cash_after=cash,default_source_instrument=source.instruments[0].instrument_id,
        default_resolved_can_short=specs['TEST'].can_short,scope='公共执行配置允许；不是所有现货计划均启用')

@probe('P16_部分退出接口实际全平')
def _():
    from tests.test_执行真实性审计 import engine, order
    runner=engine(); state=PortfolioState(); records=[]; trades=[]; details=[]; t=runner.market_data.calendar[0]
    cash,_=runner._execute_orders(t,[order(qty=10)],10000.,state,records,trades,details)
    runner._execute_orders(t,[order('exit',qty=2)],cash,state,records,trades,details)
    return dict(requested_exit=2,actual_exit=records[-1]['qty'],position_remains='TEST' in state.positions,
                scope='日线引擎exit语义为全平；永续分钟另支持风险减仓')

@probe('P17_恒价ADX与缺失递推')
def _():
    x=bars(80); x[['open','high','low','close']]=100.
    dmi=directional_movement_index(x.high,x.low,x.close,14)
    v=pd.Series([1.,2.,3.,np.nan,5.,6.,7.,8.]); avg=wilder_average(v,3)
    return dict(flat_adx_valid=int(dmi.adx.notna().sum()),flat_di_valid=int(dmi.plus_di.notna().sum()),
        wilder_after_gap_all_missing=bool(avg.iloc[3:].isna().all()),scope='不可用而不是方向证据；需明示缺失恢复策略')

@probe('P18_H4聚合仅计数无法证明连续')
def _():
    x=bars(4).reset_index(names='timestamp')
    x['timestamp']=pd.to_datetime(['2024-01-01T00:00Z','2024-01-01T00:00Z','2024-01-01T02:00Z','2024-01-01T03:00Z']); x['is_complete']=True
    r=resample_ohlcv_session(x,'H4',session_timezone='UTC',close_hour=0)
    return dict(duplicate_00_missing_01=True,marked_complete=r.is_complete.tolist(),scope='聚合边界直调；需检查上游是否先对H1去重')

@probe('P19_确定性与执行微基准')
def _():
    x=_bars(520); specs={'TEST':AssetSpec('TEST','crypto','crypto')}; hashes=[]; elapsed=[]
    for _i in range(2):
        start=time.perf_counter(); result=TurtleBacktester({'TEST':x},specs).run(); elapsed.append(time.perf_counter()-start)
        value=dict(orders=result.orders.to_json(date_format='iso'),trades=result.trades.to_json(date_format='iso'),equity=result.equity_curve.to_json(date_format='iso'))
        hashes.append(hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest())
    return dict(rows=520,run_seconds=elapsed,hashes=hashes,equal=hashes[0]==hashes[1])

@probe('P20_入口与依赖清单')
def _():
    import tomllib
    scripts=tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']['scripts']
    entries={k:dict(target=v,module_exists=importlib.util.find_spec(v.split(':')[0]) is not None) for k,v in scripts.items()}
    edges=[]; counts=Counter(); modules=[]
    for p in (ROOT/'src'/'inv_trend').rglob('*.py'):
        rel=p.relative_to(ROOT/'src'); module='.'.join(rel.with_suffix('').parts)
        if module.endswith('.__init__'): module=module[:-9]
        package=module if p.name=='__init__.py' else module.rsplit('.',1)[0]
        text=p.read_text(encoding='utf-8-sig'); tree=ast.parse(text); layer=rel.parts[1] if len(rel.parts)>2 else '__root__'
        counts[layer]+=1; modules.append(dict(path=p.relative_to(ROOT).as_posix(),lines=len(text.splitlines())))
        for node in ast.walk(tree):
            names=[]
            if isinstance(node,ast.Import): names=[n.name for n in node.names]
            elif isinstance(node,ast.ImportFrom):
                name=node.module or ''
                if node.level:
                    try: name=importlib.util.resolve_name('.'*node.level+name,package)
                    except ImportError: continue
                names=[name]
            for name in names:
                if name.startswith('inv_trend.'):
                    edges.append(dict(source=module,target=name,line=node.lineno,path=p.relative_to(ROOT).as_posix()))
    (OUT/'模块依赖.json').write_text(json.dumps(dict(entries=entries,layers=counts,modules=sorted(modules,key=lambda x:-x['lines']),edges=edges),ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(entries=entries,layer_file_counts=dict(counts),edge_count=len(edges),scope='AST静态import含相对导入；运行时注入与非字面动态导入不在图内')

@probe('P21_Daily多周期评级前缀')
def _():
    from inv_trend.application.strategy_config import DailyChecksConfig
    from inv_trend.core.strategy.daily.analysis import prepare_daily_analysis, analyze_prepared_daily_analysis
    config=DailyChecksConfig(); x=_bars(520)
    full=prepare_daily_analysis(x,config,session_anchor=x.index[0]); checks=[]
    for n in (60,132,169,237,300,419,519):
        pre=prepare_daily_analysis(x.iloc[:n],config,session_anchor=x.index[0])
        a=analyze_prepared_daily_analysis(full,position=n-1)
        b=analyze_prepared_daily_analysis(pre,position=-1)
        checks.append(dict(n=n,equal=json.dumps(a,sort_keys=True,default=str)==json.dumps(b,sort_keys=True,default=str),
                           higher_frames={k:len(v) for k,v in pre.strategy.macd_frames.items()}))
    return dict(checks=checks)

@probe('P22_完整行情价格扰动执行前缀')
def _():
    x=_bars(520); y=x.copy(); y.loc[y.index[350:],['open','high','low','close']]*=2
    specs={'TEST':AssetSpec('TEST','crypto','crypto')}; values=[]
    for frame in (x,y):
        r=TurtleBacktester({'TEST':frame},specs,liquidate_at_end=False,evaluation_end=x.index[349]).run()
        values.append(dict(orders=r.orders.to_json(date_format='iso'),trades=r.trades.to_json(date_format='iso'),equity=r.equity_curve.to_json(date_format='iso')))
    return dict(equal=values[0]==values[1],cutoff=str(x.index[349]),scope='固定截止、时间索引相同、仅改变截止后价格')

@probe('P23_未来合约事件与历史资格')
def _():
    from inv_trend.core.永续风控 import specification_at
    from tests.test_永续研究协议 import spec
    from inv_trend.application.perpetual_audit.历史资格 import HistoricalEligibility
    s=spec(); t=pd.Timestamp(s.effective_from)+pd.Timedelta(days=1)
    later=replace(s,effective_from=str(t+pd.Timedelta(days=10)),instrument_spec_version='future',exchange_max_leverage=1.)
    historical=specification_at([s],s.instrument,t)
    changed=specification_at([s,later],s.instrument,t)
    context=dict(instrument=s.instrument,available_at=str(t+pd.Timedelta(days=10)),effective_from=str(t-pd.Timedelta(days=2)),source='audit',backtest_validated=True,execution_ready=True)
    eligibility=HistoricalEligibility([s],[context])(s.instrument,t,PortfolioState(),100000.,order=None,bars=bars(),cash=100000.,equity_history=[])
    return dict(future_effective_spec_preserves_past=historical==changed,future_published_context=eligibility,
        missing_spec_available_at='available_at' not in s.__dataclass_fields__)

@probe('P24_报告独立进程断网验证')
def _():
    target=OUT/'报告重建自检.html'
    source=ROOT/'优化验证'/'修复后复验'/'完整回测结果.json'
    if not source.exists(): source=ROOT/'优化验证'/'修复后'/'完整回测结果.json'
    child='''import sys, socket, json
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from inv_trend.cli.backtest import main
from inv_trend.application.backtest.service import BacktestBatchService
def forbidden(*a, **k): raise AssertionError("forbidden data/network/backtest call")
with patch.object(socket.socket, "connect", forbidden), patch("pandas.read_parquet", forbidden), patch.object(BacktestBatchService,"run",forbidden):
    raise SystemExit(main(["report","--result",sys.argv[2],"--output",sys.argv[3]]))
'''
    result=subprocess.run([sys.executable,'-X','utf8','-c',child,str(ROOT/'src'),str(source),str(target)],cwd=OUT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
    return dict(exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr[-1800:],output_exists=target.exists(),
        source=str(source.relative_to(ROOT)),scope='独立子进程，阻断socket连接/Parquet读取/回测服务；未验证像素布局')

@probe('P25_公开阶段CLI清单')
def _():
    results=[]
    for module in ('inv_trend.cli.daily','inv_trend.cli.backtest','inv_trend.cli.market_data','inv_trend.cli.storage'):
        child='import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1),run_name="__main__")'
        result=subprocess.run([sys.executable,'-X','utf8','-c',child,str(ROOT/'src'),module,'--help'],cwd=OUT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
        results.append(dict(module=module,exit_code=result.returncode,output=(result.stdout+result.stderr)[-2500:]))
    return dict(results=results)

@probe('P26_本地数据抽样与证据范围')
def _():
    pointers=list((ROOT/'data'/'curated').glob('symbol=*/timeframe=*/current.json'))
    inventory=[]
    for symbol,tf in (('BTC','D1'),('ETH','D1'),('NVDA','D1'),('MSFT','D1'),('XAUUSD_DUKAS','H4')):
        pointer=ROOT/'data'/'curated'/f'symbol={symbol}'/f'timeframe={tf}'/'current.json'
        if not pointer.exists(): inventory.append(dict(symbol=symbol,status='missing_pointer')); continue
        ref=json.loads(pointer.read_text(encoding='utf-8')); relative=Path(ref['path'])
        p=((ROOT/relative) if relative.parts and relative.parts[0]=='data' else (ROOT/'data'/relative)).resolve()
        if not p.is_relative_to((ROOT/'data').resolve()): raise ValueError('path escapes data')
        start=time.perf_counter(); frame=pd.read_parquet(p); elapsed=time.perf_counter()-start
        stamps=pd.to_datetime(frame.timestamp,utc=True); close=pd.to_numeric(frame.close)
        factor=pd.to_numeric(frame.adjusted_close)/close if 'adjusted_close' in frame else pd.Series(dtype=float)
        inventory.append(dict(symbol=symbol,timeframe=tf,version=ref['version'],rows=len(frame),
            first=str(stamps.min()),last=str(stamps.max()),quality_status=sorted(map(str,frame.quality_status.unique())),
            incomplete=int((~frame.is_complete.astype(bool)).sum()),duplicates=int(stamps.duplicated().sum()),
            nonfinite_ohlc=int((~np.isfinite(frame[['open','high','low','close']].to_numpy(dtype=float))).sum()),
            adjusted_factor_min=float(factor.min()) if len(factor) else None,adjusted_factor_max=float(factor.max()) if len(factor) else None,
            file_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),read_parquet_calls=1,read_seconds=elapsed,
            frame_memory_bytes=int(frame.memory_usage(deep=True).sum())))
    certified=[p.relative_to(ROOT).as_posix() for p in (ROOT/'data').rglob('认证输入.json')]
    return dict(current_pointer_count=len(pointers),sample=inventory,perpetual_certified_packages_in_data=certified,
        scope='只读抽样，不调用会建目录/改catalog的DataLake构造器；未认证所有历史版本')

@probe('P27_未平仓费用与暴露指标漏计')
def _():
    from inv_trend.application.backtest.metrics import performance_metrics
    x=bars(10); runner=TurtleBacktester({'TEST':x},{'TEST':AssetSpec('TEST','crypto','crypto')},liquidate_at_end=False)
    class Strategy:
        def generate_orders_for_date(self,date,rows,state,equity,tradable_symbols):
            return [Order('TEST','open',1,1.,'audit','slow',100.,10.)] if date==x.index[0] else []
    runner.strategy=Strategy(); r=runner.run()
    m,_=performance_metrics(r.equity_curve,r.trades,r.orders,open_position_count=r.open_position_count,unrealized_pnl=r.unrealized_pnl)
    return dict(open_position_count=r.open_position_count,filled_order_cost=float(r.orders.cost.sum()),
                reported_total_cost=m['total_cost'],reported_exposure=m['exposure'],reported_turnover=m['turnover'])

after=files_hash()
evidence=dict(schema_version='1',generated_at=datetime.now(timezone.utc).isoformat(),
    python=sys.version,platform=platform.platform(),pandas=pd.__version__,numpy=np.__version__,
    git_head=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True).stdout.strip(),
    git_status=subprocess.run(['git','status','--short'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8').stdout,
    production_test_config_files_unchanged=before==after,file_hashes=after,probes=RESULTS)
(OUT/'审计证据.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,allow_nan=False,default=str),encoding='utf-8')
for r in RESULTS: print(r['id'],r['status'],r.get('message',''))
