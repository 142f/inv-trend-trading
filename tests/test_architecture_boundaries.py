from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from inv_trend.application.daily import (
    BoundDailyStage,
    DailyStage,
    STAGE_ORDER,
    StageExecutionTracker,
)


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


def test_domain_has_no_infrastructure_or_framework_dependencies() -> None:
    root = Path(__file__).parents[1] / "src" / "inv_trend" / "domain"
    banned = {
        "inv_trend.core", "inv_trend.data", "inv_trend.application",
        "inv_trend.adapters", "inv_trend.storage", "inv_trend.observability",
        "pandas", "numpy", "duckdb", "pyarrow", "sqlite3", "requests",
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
            for name in names:
                if _is_banned(name, banned):
                    violations.append(f"{source.name}: {name}")
    assert violations == []


def test_daily_stages_implement_stage_protocol() -> None:
    calls = {name: 0 for name in STAGE_ORDER}

    def executor(name: str):
        def execute() -> str:
            calls[name] += 1
            return name

        return execute

    stages = tuple(
        BoundDailyStage(name, executor(name), lambda: False)
        for name in STAGE_ORDER
    )

    assert tuple(stage.stage_name for stage in stages) == STAGE_ORDER
    assert len(set(STAGE_ORDER)) == len(STAGE_ORDER) == 6
    assert all(isinstance(stage, DailyStage) for stage in stages)
    assert tuple(stage.execute() for stage in stages) == STAGE_ORDER
    assert calls == {name: 1 for name in STAGE_ORDER}


def test_stage_execution_tracker_records_success_and_failure() -> None:
    def now() -> datetime:
        return datetime(2026, 9, 14, tzinfo=timezone.utc)

    tracker = StageExecutionTracker(now=now)
    tracker.run(BoundDailyStage("data-update", lambda: "ok", lambda: True))

    def fail() -> None:
        raise RuntimeError("broken")

    try:
        tracker.run(BoundDailyStage("strategy-screen", fail, lambda: False))
    except RuntimeError:
        pass

    assert [(row.stage_name, row.status, row.recovered, row.error_type) for row in tracker.records] == [
        ("data-update", "COMPLETED", True, None),
        ("strategy-screen", "FAILED", False, "RuntimeError"),
    ]


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


def test_observability_does_not_depend_on_data_or_strategy_adapters() -> None:
    root = Path(__file__).parents[1] / "src" / "inv_trend" / "observability"
    banned = {"inv_trend.data", "inv_trend.adapters"}
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


def test_ports_only_depend_on_pure_layers() -> None:
    """Interface contracts must stay importable from every layer below them."""

    root = Path(__file__).parents[1] / "src" / "inv_trend" / "ports"
    banned = {
        "inv_trend.application", "inv_trend.adapters", "inv_trend.data",
        "inv_trend.storage", "inv_trend.observability", "inv_trend.monitoring",
        "inv_trend.notifications", "inv_trend.cli",
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


def test_adapters_do_not_depend_on_the_application_package() -> None:
    """Adapter code must never import the use-case layer above it.

    ``adapters -> observability`` is deliberately *not* checked here.  The
    presentation package owns rendering only — it is already forbidden from
    importing ``data`` or ``adapters`` — so a filesystem publisher composing an
    HTML renderer is a *downward* dependency, not a reversed one; forcing it
    out would require the application layer (which may not import
    ``observability``) to build the default renderer, and direct
    ``DailyMarketScanService`` constructions rely on that default.  The exact
    package graph, including that edge, is pinned by ``DECLARED_PACKAGE_EDGES``
    above, so the pair still cannot grow.
    """

    root = Path(__file__).parents[1] / "src" / "inv_trend" / "adapters"
    observed: set[tuple[str, str]] = set()
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
                if _is_banned(name, {"inv_trend.application"}):
                    observed.add((source.relative_to(root).as_posix(), name))

    assert observed == set()


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
    assert {
        "domain", "core", "data", "application", "adapters", "integrations",
        "observability", "cli",
    } <= {
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


# --- Package-level dependency contract -------------------------------------
#
# ``DECLARED_PACKAGE_EDGES`` is the complete, reviewed package-to-package import
# graph.  A new pair must be deliberate, so the test fails until it is added
# here; this is the structure-refactor counterpart of the whitespace-forbidden
# ``adapters`` allowlist below.
#
# ``KNOWN_PACKAGE_CYCLES`` is the shrink-only progress counter for the refactor:
# it lists the bidirectional package pairs still awaiting removal and must
# eventually become empty.

_SOURCE_ROOT = Path(__file__).parents[1] / "src" / "inv_trend"

DECLARED_PACKAGE_EDGES: frozenset[tuple[str, str]] = frozenset({
    ("adapters", "config"),
    ("adapters", "core"),
    ("adapters", "data"),
    ("adapters", "domain"),
    ("adapters", "observability"),
    ("adapters", "ports"),
    ("application", "adapters"),
    ("application", "config"),
    ("application", "core"),
    ("application", "data"),
    ("application", "domain"),
    ("application", "ports"),
    ("application", "storage"),
    ("cli", "adapters"),
    ("cli", "application"),
    ("cli", "config"),
    ("cli", "core"),
    ("cli", "data"),
    ("cli", "monitoring"),
    ("cli", "notifications"),
    ("cli", "observability"),
    ("cli", "storage"),
    ("data", "core"),
    ("data", "storage"),
    ("integrations", "adapters"),
    ("monitoring", "adapters"),
    ("monitoring", "core"),
    ("monitoring", "data"),
    ("monitoring", "observability"),
    ("notifications", "monitoring"),
    ("notifications", "observability"),
    ("observability", "core"),
    ("ports", "core"),
    ("storage", "adapters"),
    ("storage", "application"),
    ("storage", "core"),
    ("storage", "data"),
})

KNOWN_PACKAGE_CYCLES: frozenset[frozenset[str]] = frozenset({
    frozenset({"application", "storage"}),
    frozenset({"data", "storage"}),
})


def _imported_modules(source: Path) -> list[str]:
    """Return every literal ``inv_trend.*`` module name imported by ``source``."""

    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(_absolute_import_target(node, source))
        elif isinstance(node, ast.Call):
            found.extend(_dynamic_import_names(node))
    return [item for item in found if item]


def _absolute_import_target(node: ast.ImportFrom, source: Path) -> str:
    """Resolve a (possibly relative) ``from`` import to its absolute module."""

    if not node.level:
        return node.module or ""
    package = ["inv_trend", *source.relative_to(_SOURCE_ROOT).parts[:-1]]
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def _package_edges() -> set[tuple[str, str]]:
    """Return the distinct importer-package -> imported-package edge set."""

    edges: set[tuple[str, str]] = set()
    for source in _SOURCE_ROOT.rglob("*.py"):
        importer = source.relative_to(_SOURCE_ROOT).parts[0]
        for target in _imported_modules(source):
            if not target.startswith("inv_trend."):
                continue
            imported = target.split(".")[1]
            if imported != importer:
                edges.add((importer, imported))
    return edges


def test_package_dependency_graph_matches_the_declared_contract() -> None:
    """The package graph is exactly the reviewed set; drift must be deliberate."""

    assert _package_edges() == set(DECLARED_PACKAGE_EDGES)


def test_only_the_declared_package_cycles_remain() -> None:
    """Bidirectional package pairs are the refactor's measurable counter."""

    edges = _package_edges()
    cycles = {frozenset(pair) for pair in edges if (pair[1], pair[0]) in edges}
    assert cycles == set(KNOWN_PACKAGE_CYCLES), (
        "package cycle set changed; update KNOWN_PACKAGE_CYCLES only when a cycle "
        f"has been removed, never added: {sorted(tuple(sorted(c)) for c in cycles)}"
    )
