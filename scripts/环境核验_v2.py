"""显示实际依赖状态，不用伪Parquet替代缺失后端。"""
import argparse
import json
from pathlib import Path
import sys

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--strict',action='store_true')
    a=p.parse_args();sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from inv_trend.application.数据认证 import environment_status
    status=environment_status();print(json.dumps(status,ensure_ascii=False,indent=2))
    return int(a.strict and not status['full_project_dependencies_ready'])

if __name__=='__main__':raise SystemExit(main())
