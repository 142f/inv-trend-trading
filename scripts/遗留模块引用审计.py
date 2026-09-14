"""Read-only audit for deciding whether compatibility modules may be removed."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def audit(root: Path, targets: tuple[str, ...]) -> dict[str, list[str]]:
    result = {target: [] for target in targets}
    for path in (*root.joinpath("src").rglob("*.py"), *root.joinpath("tests").rglob("*.py")):
        for imported in imported_modules(path):
            for target in targets:
                if imported == target or imported.startswith(target + "."):
                    result[target].append(path.relative_to(root).as_posix())
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        for target in targets:
            if target in text:
                result[target].append("pyproject.toml")
    return {target: sorted(set(paths)) for target, paths in result.items()}


def classify(result: dict[str, list[str]]) -> dict[str, dict[str, object]]:
    """Classify removal readiness without inferring status from a version suffix."""
    classified = {}
    for target, references in result.items():
        production = [path for path in references if path.startswith("src/") or path == "pyproject.toml"]
        tests = [path for path in references if path.startswith("tests/")]
        status = "正式入口" if production else "兼容入口" if tests else "无引用模块"
        classified[target] = {"status": status, "production_references": production,
                              "test_references": tests, "deletable": not references}
    return classified


def main() -> int:
    parser = argparse.ArgumentParser(description="审计兼容模块的静态引用；不修改文件")
    parser.add_argument("modules", nargs="+", help="完整模块名")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = audit(args.root.resolve(), tuple(args.modules))
    print(json.dumps(classify(result), ensure_ascii=False, indent=2))
    return 1 if any(result.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
