"""薄适配：复用现有v3存储治理实现，修复原来不存在的console入口。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from inv_trend.storage.结构化存储_v3 import UnifiedStore
from inv_trend.storage.数据生命周期_v3 import cleanup, ready_to_replace, backup_database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("summary", "verify", "clean", "backup"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--strict-parquet", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-age-days", type=int, default=7)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "summary":
        store = UnifiedStore(args.data)
        result = {"authority": str(store.path), "space": store.rows("SELECT * FROM v_storage_usage")}
    elif args.command == "verify":
        result = ready_to_replace(args.data, strict_parquet=args.strict_parquet)
    elif args.command == "clean":
        result = cleanup(args.data, apply=args.apply, min_age_days=args.min_age_days)
    else:
        if args.destination is None:
            parser.error("backup requires --destination")
        result = {"database_backup": str(backup_database(args.data, args.destination)),
                  "note": "Only the SQLite DB is backed up; immutable files require a separate verified copy."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(isinstance(result, dict) and result.get("ok") is False)


if __name__ == "__main__":
    raise SystemExit(main())
