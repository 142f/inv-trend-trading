from __future__ import annotations

import ast
from pathlib import Path


def test_core_does_not_depend_on_io_or_application_packages() -> None:
    root = Path(__file__).parents[1] / "src" / "inv_trend" / "core"
    banned = {
        "inv_trend.data", "inv_trend.application", "inv_trend.observability",
        "inv_trend.adapters.detector", "inv_trend.adapters.multi_asset", "sqlite3", "requests", "urllib",
    }
    violations = []
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call):
                names = _dynamic_import_names(node)
            for name in names:
                if _is_banned(name, banned):
                    violations.append(f"{source.name}: {name}")
    assert violations == []


def test_application_does_not_depend_on_cli_or_presentation_packages() -> None:
    root = Path(__file__).parents[1] / "src" / "inv_trend" / "application"
    banned = {"argparse", "rich", "webbrowser", "inv_trend.cli", "inv_trend.observability"}
    violations = []
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call):
                names = _dynamic_import_names(node)
            for name in names:
                if _is_banned(name, banned):
                    violations.append(f"{source.name}: {name}")
    assert violations == []


def test_historical_data_does_not_depend_on_strategy_or_application_packages() -> None:
    root = Path(__file__).parents[1] / "src" / "inv_trend" / "data"
    banned = {"inv_trend.application", "inv_trend.adapters.detector", "inv_trend.adapters.multi_asset"}
    violations = []
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call):
                names = _dynamic_import_names(node)
            for name in names:
                if _is_banned(name, banned):
                    violations.append(f"{source.name}: {name}")
    assert violations == []


def test_source_has_one_canonical_package_namespace() -> None:
    repository = Path(__file__).parents[1]
    source = repository / "src" / "inv_trend"
    assert {"core", "data", "application", "adapters", "integrations", "observability", "cli"} <= {
        path.name for path in source.iterdir() if path.is_dir()
    }
    assert all(
        not (repository / legacy).exists()
        for legacy in (
            "historical_data",
            "inv_trend_core",
            "inv_trend_application",
            "inv_trend_observability",
            "inv_trend_integrations",
            "turtle_detector",
            "turtle_multi_asset",
        )
    )


def _is_banned(name: str, banned: set[str]) -> bool:
    return any(name == package or name.startswith(f"{package}.") for package in banned)


def _dynamic_import_names(node: ast.Call) -> list[str]:
    """Return literal module names passed to Python's dynamic import hooks."""

    function_name = (
        node.func.id
        if isinstance(node.func, ast.Name)
        else node.func.attr
        if isinstance(node.func, ast.Attribute)
        else None
    )
    if function_name not in {"__import__", "import_module"} or not node.args:
        return []
    first = node.args[0]
    return [first.value] if isinstance(first, ast.Constant) and isinstance(first.value, str) else []
