"""滚动验证唯一复现入口；禁止覆盖已有研究目录。"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    p.add_argument('--output',required=True)
    p.add_argument('--iterations',type=int)
    p.add_argument('--candidate-limit',type=int)
    p.add_argument('--framework-label')
    p.add_argument('--skip-stress',action='store_true')
    a=p.parse_args(argv);sys.path.insert(0,str(Path(a.root)/'src'))
    from inv_trend.application.滚动验证 import run_research
    run_research(a.root,a.output,iterations=a.iterations,candidate_limit=a.candidate_limit,
                 framework_label=a.framework_label,skip_stress=a.skip_stress)
    from inv_trend.observability.滚动报告 import render_report
    render_report(a.output,Path(a.output)/'中文滚动回测报告_v2.html')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
