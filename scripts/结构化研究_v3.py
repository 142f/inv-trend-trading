"""唯一数据库版滚动研究入口：复用v2策略/B6，不修改规则、不择优升级。"""
from __future__ import annotations
import argparse,json
from pathlib import Path
import sys

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--study',required=True,help='单层、唯一中文运行名，不能覆盖历史研究')
    parser.add_argument('--iterations',type=int)
    parser.add_argument('--candidate-limit',type=int)
    parser.add_argument('--skip-stress',action='store_true')
    a=parser.parse_args(argv);root=a.root.resolve();sys.path.insert(0,str(root/'src'))
    from inv_trend.storage.结构化存储_v3 import UnifiedStore,normalize_ref
    if normalize_ref(a.study)!=a.study or '/' in a.study:parser.error('study必须是安全的单层名称')
    store=UnifiedStore(root/'data')
    from inv_trend.application.滚动验证 import run_research
    from inv_trend.observability.滚动报告 import render_report
    from inv_trend.storage.滚动核验 import verify_research
    output=store.root/'backtests'/a.study
    run_research(root,output,iterations=a.iterations,candidate_limit=a.candidate_limit,
                 framework_label='B6：存储v3，不改交易语义',skip_stress=a.skip_stress)
    # HTML is a reproducible derivative; register its bytes, not a second result authority.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='中文报告_') as temporary:
        report=Path(temporary)/'中文回测报告_v3.html'
        render_report(output,report)
        logical=f'reports/{a.study}/中文回测报告_v3.html'
        store.put_object(logical,report.read_bytes(),stage='reports')
        store.audit('PUBLISH_REPORT',logical,{'study':store.key(output)})
    check=verify_research(output)
    physical=store.resolve(logical)
    print(json.dumps({'研究':store.key(output),'报告':str(physical),'核验':check},ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
