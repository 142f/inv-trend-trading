"""固定研究族的配对区块Bootstrap及严格升级门禁；不将诊断当成新盲测。"""
from __future__ import annotations
import numpy as np
import pandas as pd


def family_bootstrap(returns, *, repetitions=1000, block_length=20, seed=20260907):
    frame=pd.DataFrame(returns).fillna(0.)
    values=frame.to_numpy(float); n,k=values.shape
    if n<2 or not np.isfinite(values).all():raise ValueError('收益序列缺失或过短')
    if repetitions<99 or block_length<1 or block_length>n:raise ValueError('Bootstrap参数无效')
    rng=np.random.default_rng(seed)
    mu=values.mean(axis=0); sigma=values.std(axis=0,ddof=1)
    valid=sigma>1e-14;den=np.where(valid,sigma,1.)
    statistic=np.where(valid,np.sqrt(n)*mu/den,0.)
    centered=values-mu
    differences=values-values[:,[0]]
    dmean=differences.mean(axis=0);dstd=differences.std(axis=0,ddof=1)
    dvalid=dstd>1e-14;dden=np.where(dvalid,dstd,1.)
    dstat=np.where(dvalid,np.sqrt(n)*dmean/dden,0.)
    dcenter=differences-dmean
    means=[];nullmax=[];diffmax=[]
    blocks=int(np.ceil(n/block_length));offset=np.arange(block_length)
    for _ in range(repetitions):
        idx=((rng.integers(0,n,size=blocks)[:,None]+offset)%n).ravel()[:n]
        means.append(values[idx].mean(axis=0))
        nullmax.append(np.max(np.where(valid,np.sqrt(n)*centered[idx].mean(axis=0)/den,0.)))
        diffmax.append(np.max(np.where(dvalid,np.sqrt(n)*dcenter[idx].mean(axis=0)/dden,0.)))
    means=np.asarray(means);nullmax=np.asarray(nullmax);diffmax=np.asarray(diffmax)
    rows=[]
    for j,name in enumerate(frame.columns):
        rows.append(dict(iteration=str(name),daily_mean=float(mu[j]),
            daily_mean_ci95=[float(x) for x in np.quantile(means[:,j],[.025,.975])],
            adjusted_p_vs_zero=float((1+np.sum(nullmax>=statistic[j]))/(repetitions+1)) if valid[j] else 1.,
            adjusted_p_vs_baseline=float((1+np.sum(diffmax>=dstat[j]))/(repetitions+1)) if dvalid[j] else 1.,
            samples=n,block_calendar_days=block_length,repetitions=repetitions,seed=seed))
    return dict(method='配对循环区块Bootstrap；固定研究族单步max-t近似校正',
        caveat='只覆盖本次公开的候选池；未知历史搜索、固定品种池及研究者已见历史的偏差没有被消除',results=rows)


def promotion_decision(config, baseline, candidate, statistics, stress):
    """布尔门槛全部通过才允许升级；不提供绕过认证的force选项。"""
    a,b=baseline['metrics'],candidate['metrics']
    gates={
        '确有未见前瞻数据':not config['history_previously_inspected'],
        '交易成本后累计收益为正':b['total_return']>0,
        '累计收益不劣于预先冻结基准':b['total_return']>=a['total_return'],
        '最大回撤不劣于基准':b['max_drawdown']<=a['max_drawdown'],
        'Sharpe不劣于基准':b['sharpe_ratio'] is not None and a['sharpe_ratio'] is not None and b['sharpe_ratio']>=a['sharpe_ratio'],
        'Calmar不劣于基准':b['calmar_ratio'] is not None and a['calmar_ratio'] is not None and b['calmar_ratio']>=a['calmar_ratio'],
        '正收益滚动窗口不少于六成':b['positive_fold_ratio']>=config['upgrade']['min_positive_fold_ratio'],
        '成本压力后仍为正':bool(stress) and all(x['metrics']['total_return']>0 for x in stress),
        '相对基准经多重比较校正后显著':statistics['adjusted_p_vs_baseline']<config['statistical_audit']['alpha'],
        '执行与市场数据已经认证':bool(config['execution']['certified']),
    }
    return dict(gates={k:bool(v) for k,v in gates.items()},promoted=bool(all(gates.values())),
        active_reference='候选01：此前冻结v4；不因本次外层收益改选',
        research_implementation='滚动策略_v2：单一实现＋不可变参数合同',
        production_enabled=False,
        decision='未达到升级门槛，保留基准，不宣称稳定正期望' if not all(gates.values()) else '可进入独立发布审批')
