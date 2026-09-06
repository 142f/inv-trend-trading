"""Compare fixed parameters, confirm reproducibility, and benchmark an equivalent kernel."""
from __future__ import annotations

import json
import argparse
from pathlib import Path
from statistics import median
import subprocess
import sys
from time import perf_counter
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--reuse-benchmark",action="store_true")
    args=parser.parse_args()
    output=ROOT/"优化验证"
    def read(label, name):
        return json.loads((output/label/name).read_text(encoding="utf-8"))
    before,after,repeat,stress=[read(label,"对比摘要.json") for label in ("修改前","修复后","修复后复验","双倍交易成本")]
    original_plan,final_plan=read("修改前","实验锁定.json"),read("修复后","实验锁定.json")
    assert original_plan["data"]==final_plan["data"]
    assert original_plan["plan_sha256"]==final_plan["plan_sha256"]
    assert after["result_hash"]==repeat["result_hash"],"reproducibility failed"
    by_before={item["id"]:item for item in before["combinations"]}
    by_stress={item["id"]:item for item in stress["combinations"]}
    comparisons=[]
    for item in after["combinations"]:
        old=by_before[item["id"]]
        comparisons.append(dict(id=item["id"],stop_n=item["parameters"]["rules.stop_n"],
                                pyramid_step_n=item["parameters"]["rules.pyramid_step_n"],
                                before={key:old[key] for key in ("metrics","oos","holdout")},
                                after={key:item[key] for key in ("metrics","oos","holdout")},
                                stress_holdout=by_stress[item["id"]]["holdout"]))
    kernel=(json.loads((output/"前后对比证据.json").read_text(encoding="utf-8"))["kernel_benchmark"]
            if args.reuse_benchmark else benchmark())
    result=dict(reproducible=True,result_hash=after["result_hash"],data_identical=True,
                parameter_grid_identical=True,default_parameters={"stop_n":2.,"pyramid_step_n":.5},
                confirmed_candidate=after["best"],production_defaults_changed=False,
                elapsed_seconds=dict(before=before["seconds"],after=after["seconds"]),
                html_bytes=dict(before=before["html_bytes"],after=after["html_bytes"]),
                comparisons=comparisons,kernel_benchmark=kernel)
    (output/"前后对比证据.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(reproducible=True,kernel=kernel),ensure_ascii=False,indent=2))
    for item in comparisons:
        print(item['stop_n'],item['pyramid_step_n'],
              {side:{key:round(item[side]['metrics'][key],4) for key in ('annualized_return','max_drawdown','sharpe_ratio','trade_count')}
               for side in ('before','after')},'holdout',round(item['after']['holdout']['total_return'],4))


def benchmark():
    from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
    from inv_trend.adapters.multi_asset.models.domain import TurtleRules
    path="src/inv_trend/adapters/multi_asset/strategy/engine.py"
    source=subprocess.check_output(["git","show",f"332f70e69458b931feeeaacd79fa5cb0aefb95d8:{path}"],cwd=ROOT).decode('utf-8')
    module=types.ModuleType('inv_trend.adapters.multi_asset.strategy._audit_baseline')
    exec(compile(source,path,'exec'),module.__dict__)
    old,new=module.MultiAssetTurtleStrategy({},TurtleRules()),MultiAssetTurtleStrategy({},TurtleRules())
    rows=[dict(high_20=105.,low_20=95.,close=float(90+i%21),high=float(91+i%21),low=float(89+i%21)) for i in range(50000)]
    def evaluate(engine):return [engine._breakout_signal(row,20,2.) for row in rows]
    assert evaluate(old)==evaluate(new)
    samples=[[],[]]
    for run in range(7):
        for i in ((0,1) if run%2==0 else (1,0)):
            start=perf_counter()
            evaluate((old,new)[i])
            samples[i].append(perf_counter()-start)
    return dict(rows=len(rows),runs=7,equivalent=True,before_seconds=median(samples[0]),
                after_seconds=median(samples[1]),speedup=median(samples[0])/median(samples[1]),samples=samples)


if __name__=='__main__':
    main()
