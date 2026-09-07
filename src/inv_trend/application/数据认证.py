"""数据和依赖的可验证边界。未认证不等于数据一定错误，也不等于可以实盘。"""
from __future__ import annotations
import importlib.util
from importlib.metadata import version, PackageNotFoundError
import numpy as np
import pandas as pd


def environment_status():
    result={}
    for package in ('numpy','pandas','pytest','pyarrow','duckdb','ruff'):
        try:value=version(package)
        except PackageNotFoundError:value=None
        result[package]={'available':importlib.util.find_spec(package) is not None,'version':value}
    return {'packages':result,'walk_forward_dependencies_ready':all(result[x]['available'] for x in ('numpy','pandas')),
            'full_project_dependencies_ready':all(x['available'] for x in result.values()),
            'parquet_emulation_used':False}


def assess_data(data, config):
    symbols=[]
    for symbol,frame in data.items():
        returns=frame.close.pct_change()
        symbols.append(dict(symbol=symbol,bars=len(frame),first_label=str(frame.index[0]),
            last_label=str(frame.index[-1]),zero_volume_bars=int(frame.volume.eq(0).sum()),
            abs_return_over_30pct=int(returns.abs().ge(.30).sum()),
            max_abs_return=float(returns.abs().max()),
            calendar_certified=False,point_in_time_universe_certified=False,
            adjustment_certified=False,contract_certified=False,
            volume_is_execution_volume=config['specs'][symbol]['asset_class']=='equity'))
    return dict(data_version=config['dataset_sha256'],symbols=symbols,production_ready=False,
        interpretation='OHLC格式校验通过不替代时段、复权、合约和点时成份认证；未静默删改异常涨跌',
        known_limitations=config['limitations'])
