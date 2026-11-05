"""CLI for listing, exporting and previewing the fixed HTML showcase pages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from inv_trend.observability.静态展示 import PAGES, export_all, export_page, serve


def _list(as_json: bool) -> int:
    rows = [
        {"id": page.id, "title": page.title, "kind": page.kind, "path": page.path}
        for page in PAGES
    ]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for row in rows:
        print(f"{row['id']}\t{row['kind']}\t{row['title']}\t{row['path']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list fixed showcase pages")
    listing.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="export fixed pages and available generated files")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--page", choices=[page.id for page in PAGES])
    export.add_argument("--root", type=Path, default=Path.cwd())

    preview = sub.add_parser("serve", help="serve a read-only local preview")
    preview.add_argument("--port", type=int, default=8765)
    preview.add_argument("--root", type=Path, default=Path.cwd())

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _list(args.json)
        if args.command == "export":
            if args.page:
                written = export_page(args.page, args.output, root=args.root)
                print(json.dumps({
                    "written": [str(path) for path in written],
                    "missing": [],
                }, ensure_ascii=False))
            else:
                result = export_all(args.output, root=args.root)
                print(json.dumps({
                    "written": [str(path) for path in result.written],
                    "missing": list(result.missing),
                }, ensure_ascii=False))
            return 0
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        serve(args.root, args.port)
        return 0
    except (KeyError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
