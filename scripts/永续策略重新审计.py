"""Reproducible public-data audit; never imports private OKX credentials."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='下载交易所可提供的最早历史')
    parser.add_argument('--max-pages', type=int, help='仅调试；截断的数据不能认证为完整')
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.max_pages is not None and args.max_pages < 1:
        parser.error('--max-pages must be positive')
    from inv_trend.application.perpetual_audit.审计服务 import run_audit
    result = run_audit(ROOT, data_root=args.data_root, output=args.output,
                       download=args.download, max_pages=args.max_pages)
    print(json.dumps({k: result[k] for k in ('evidence_level', 'conclusion', 'registry')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
