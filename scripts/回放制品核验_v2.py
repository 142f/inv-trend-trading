"""核验已完成回放的全部行情版本、交易制品与参数锁；失败返回非零状态。"""
from pathlib import Path
import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from inv_trend.storage.滚动核验 import verify_research
    print(json.dumps(verify_research(args.directory), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
