#!/usr/bin/env python
"""结构重构「改前 vs 改后」产物一致性比较器（v1）。

判定标准（方案 §3.4 / 决策 D5）：先按白名单 N1–N7 归一化，再要求**严格相等**。
白名单只覆盖契约声明的易变/溯源字段；数值指标、内容寻址哈希、事件与订单记录、
列序、dtype、行数与行序一律不归一化。

用法::

    python scripts/结构对比_v1.py --before <dir> --after <dir> [--out <json>]
    python scripts/结构对比_v1.py --self-test
    python scripts/结构对比_v1.py --junit-before <xml> --junit-after <xml>

退出码: 0 = 归一化后全部相等; 2 = 存在非易变字段差异; 3 = 存在结构差异。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# §3.4 归一化白名单
# ---------------------------------------------------------------------------

# N4：JSON 键名命中易变集合，且值形如时间戳/日期时，值替换为固定 token。
VOLATILE_KEYS = frozenset(
    {"generated_at", "created_at", "updated_at", "evaluated_at", "now", "timestamp"}
)

# N7：树级溯源字段。它们描述的是**代码树**而不是产物行为，必然不同；
# 归一化它们不会掩盖任何数值/哈希/事件差异（见 S9 review 第 1 次口径）。
PROVENANCE_KEYS = frozenset({"tree_commit", "python_module_count"})

# N2：RFC3339/ISO 时间戳（含 T 或空格分隔、可选秒/小数/时区）。
_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
# N3：裸 ISO 日期（整串匹配时才替换）。
_DATE_FULL_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# N6：`artifacts/<uuid>` 一类的自增/随机目录名。刻意保持窄，
# 避免误伤 32/64 位内容寻址哈希（result_hash / input_key / decision_id）。
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_TMPDIR_RE = re.compile(r"\.tmp-[0-9a-f]{6,}")

# N5：Windows 绝对路径字符串。POSIX 路径不在此列，避免误伤 HTML 中的 "/tag"。
_WIN_PATH_RE = re.compile(r"(?<![\w:])[A-Za-z]:\\[^\s\"'<>|,;]+")

_BINARY_SUFFIXES = {".parquet", ".png", ".jpg", ".jpeg", ".zip", ".gz", ".xlsx", ".sqlite3"}


def _norm_text(text: str, reps: list[tuple[str, str]]) -> str:
    """按 N1 → N6 → N2 → N5 顺序归一化一段文本。

    N1 的前缀替换按路径**长度降序**施加，使结果与传入顺序无关：否则较短的
    共同祖先前缀（例如仓库根 «TREE»）会先吃掉较深的产物根前缀（«OUT»/«IN»）。
    """

    for raw, token in sorted(reps, key=lambda item: len(item[0]), reverse=True):  # N1
        text = text.replace(raw, token)
        text = text.replace(raw.replace("\\", "/"), token)
    text = _TMPDIR_RE.sub("«ID»", text)  # N6
    text = _UUID_RE.sub("«ID»", text)
    text = _TS_RE.sub("«TS»", text)  # N2
    text = _WIN_PATH_RE.sub("«PATH»", text)  # N5
    return text


def _norm_json(value, reps: list[tuple[str, str]], key: str | None = None):
    """递归归一化 JSON 值；不做任何删除，只做替换，保证结构仍被比对。"""

    if isinstance(value, dict):
        result = {}
        for item_key, item_value in value.items():
            if item_key in PROVENANCE_KEYS:  # N7
                result[item_key] = "«PROVENANCE»"
            else:
                result[item_key] = _norm_json(item_value, reps, str(item_key))
        return result
    if isinstance(value, list):
        return [_norm_json(item, reps, None) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if key in VOLATILE_KEYS and (
            _TS_RE.fullmatch(stripped) or _DATE_FULL_RE.fullmatch(stripped)
        ):
            return "«VOLATILE»"  # N4
        if _DATE_FULL_RE.fullmatch(stripped):
            return "«DATE»"  # N3
        return _norm_text(value, reps)
    return value


# ---------------------------------------------------------------------------
# 首个差异定位
# ---------------------------------------------------------------------------


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _leaf_diffs(
    left, right, path: str = "$", key: str | None = None
) -> list[tuple[str, str, object, object]]:
    """返回 (父路径, 键, before值, after值) 形式的全部差异叶子。

    摘要集合文件的键本身就是被摘要制品的相对路径，因此必须保留**原始键名**，
    不能从 JSON path 里反推。
    """

    if isinstance(left, dict) and isinstance(right, dict):
        found: list[tuple[str, str, object, object]] = []
        for item_key in sorted(set(left) | set(right)):
            if item_key not in left or item_key not in right:
                found.append((path, item_key, "<缺失>", "<缺失>"))
                continue
            found.extend(_leaf_diffs(left[item_key], right[item_key], path, item_key))
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        found = []
        for index, (a, b) in enumerate(zip(left, right)):
            found.extend(_leaf_diffs(a, b, f"{path}[{index}]", key))
        return found
    if left != right:
        return [(path, key if key is not None else path.rsplit(".", 1)[-1], left, right)]
    return []


def _first_json_diff(left, right, path: str = "$") -> str | None:
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            missing = sorted(set(left) - set(right))
            unexpected = sorted(set(right) - set(left))
            return f"{path}: 键集合不同; 仅before={missing}, 仅after={unexpected}"
        for key in left:
            found = _first_json_diff(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path}: 长度不同; before={len(left)}, after={len(right)}"
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_json_diff(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return f"{path}: before={left!r}, after={right!r}"
    return None


def _first_text_diff(left: str, right: str) -> str | None:
    left_lines, right_lines = left.splitlines(), right.splitlines()
    if len(left_lines) != len(right_lines):
        return f"行数不同: before={len(left_lines)}, after={len(right_lines)}"
    for index, (a, b) in enumerate(zip(left_lines, right_lines), start=1):
        if a != b:
            column = next(
                (i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b))
            )
            return (
                f"第 {index} 行第 {column + 1} 列: "
                f"before={a[max(0, column - 20):column + 20]!r}, "
                f"after={b[max(0, column - 20):column + 20]!r}"
            )
    return "文本相同但字节不同（可能为行尾/编码差异）"


# ---------------------------------------------------------------------------
# 分派比对
# ---------------------------------------------------------------------------


def _frames_equal(left, right) -> tuple[bool, str]:
    import pandas as pd

    try:
        pd.testing.assert_frame_equal(
            left, right, check_exact=True, check_dtype=True, check_like=False
        )
        return True, ""
    except AssertionError as exc:  # noqa: PERF203 - 仅在差异分支构造证据
        first = str(exc).splitlines()
        return False, " | ".join(first[:3])


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _compare_one(
    rel: str, left_path: Path, right_path: Path, reps_before, reps_after
) -> dict[str, object]:
    import pandas as pd

    suffix = Path(rel).suffix.lower()
    left_bytes, right_bytes = left_path.read_bytes(), right_path.read_bytes()
    raw_equal = left_bytes == right_bytes

    if suffix in {".parquet"}:
        left_frame = pd.read_parquet(left_path)
        right_frame = pd.read_parquet(right_path)
        equal, detail = _frames_equal(left_frame, right_frame)
        return {
            "rel": rel,
            "kind": "parquet",
            "raw_equal": raw_equal,
            "normalized_equal": equal,
            "category": "equal" if equal else "value",
            "first_diff": None if equal else detail,
        }

    if suffix == ".json":
        left_value = json.loads(left_bytes.decode("utf-8"))
        right_value = json.loads(right_bytes.decode("utf-8"))
        left_norm = _norm_json(left_value, reps_before)
        right_norm = _norm_json(right_value, reps_after)
        equal = left_norm == right_norm
        row = {
            "rel": rel,
            "kind": "json",
            "raw_equal": raw_equal,
            "normalized_equal": equal,
            "category": _category(raw_equal, equal),
            "first_diff": None if equal else _first_json_diff(left_norm, right_norm),
        }
        if not equal:
            diffs = _leaf_diffs(left_norm, right_norm)
            digest_keys = [
                key
                for (_parent, key, before_value, after_value) in diffs
                if isinstance(before_value, str)
                and isinstance(after_value, str)
                and _DIGEST_RE.fullmatch(before_value)
                and _DIGEST_RE.fullmatch(after_value)
            ]
            # 只有「全部差异叶子都是摘要」时才允许走派生摘要归因，见 compare_trees 的闭包校验。
            row["digest_only"] = bool(diffs) and len(digest_keys) == len(diffs)
            row["digest_keys"] = digest_keys
        return row

    if suffix == ".csv":
        left_text = _norm_text(_read_text(left_path), reps_before)
        right_text = _norm_text(_read_text(right_path), reps_after)
        text_equal = left_text == right_text
        # 额外数值列精确校验：在**归一化后**的文本上重建 DataFrame，
        # 这样路径/时间戳列被 N1/N2 抹平，而数值列仍必须逐位精确相等。
        detail = ""
        frame_equal = True
        if text_equal:
            try:
                frame_equal, detail = _frames_equal(
                    pd.read_csv(io.StringIO(left_text)), pd.read_csv(io.StringIO(right_text))
                )
            except Exception as exc:  # noqa: BLE001 - 解析失败即为差异
                frame_equal, detail = False, f"归一化后 CSV 无法解析对比: {exc}"
        equal = text_equal and frame_equal
        return {
            "rel": rel,
            "kind": "csv",
            "raw_equal": raw_equal,
            "normalized_equal": equal,
            "category": _category(raw_equal, equal),
            "first_diff": None
            if equal
            else (detail or _first_text_diff(left_text, right_text)),
        }

    if suffix in {".html", ".htm", ".txt", ".md", ".yaml", ".yml", ".log"}:
        left_text = _norm_text(_read_text(left_path), reps_before)
        right_text = _norm_text(_read_text(right_path), reps_after)
        equal = left_text == right_text
        return {
            "rel": rel,
            "kind": "text",
            "raw_equal": raw_equal,
            "normalized_equal": equal,
            "category": _category(raw_equal, equal),
            "first_diff": None if equal else _first_text_diff(left_text, right_text),
        }

    # 其它：仅做路径前缀归一化后的字节比较。
    left_text = _norm_text(left_bytes.decode("utf-8", errors="replace"), reps_before)
    right_text = _norm_text(right_bytes.decode("utf-8", errors="replace"), reps_after)
    equal = left_text == right_text
    return {
        "rel": rel,
        "kind": "bytes" if suffix in _BINARY_SUFFIXES else "other",
        "raw_equal": raw_equal,
        "normalized_equal": equal,
        "category": _category(raw_equal, equal),
        "first_diff": None
        if equal
        else f"前 64 字节 sha256: before={hashlib.sha256(left_bytes).hexdigest()[:16]}, "
        f"after={hashlib.sha256(right_bytes).hexdigest()[:16]}",
    }


def _category(raw_equal: bool, normalized_equal: bool) -> str:
    if raw_equal:
        return "equal"
    return "volatile_only" if normalized_equal else "value"


# ---------------------------------------------------------------------------
# 目录级比对
# ---------------------------------------------------------------------------


def _collect(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path
    return files


def _resolve_digest_target(rel: str, key: str, by_rel: dict[str, dict]) -> str | None:
    """把摘要集合文件里的键解析成同树中的被摘要制品相对路径。"""

    base = Path(rel).parent
    for candidate_base in (base, base.parent, base.parent.parent):
        candidate = (candidate_base / key).as_posix()
        if candidate in by_rel:
            return candidate
    return None


def _apply_digest_closure(results: list[dict[str, object]]) -> list[str]:
    """派生摘要归因：若某 JSON 的全部差异叶子都是 sha256 摘要，且每个摘要所指
    制品本身已被判定为 `volatile_only`（即只含白名单易变字段差异），则该文件
    的差异也是派生的、不携带独立行为信息，改判为 `volatile_only`。

    这一步**不归一化任何摘要**：它只做归因证明。只要有任一摘要指向真实差异
    制品、或无法解析，就保持 `value`。
    """

    by_rel = {str(row["rel"]): row for row in results}
    reclassified: list[str] = []
    for row in results:
        if row.get("category") != "value" or row.get("kind") != "json":
            continue
        if not row.get("digest_only"):
            continue
        keys = [str(key) for key in (row.get("digest_keys") or [])]
        targets: list[str] = []
        unresolved = False
        for key in keys:
            target = _resolve_digest_target(str(row["rel"]), key, by_rel)
            if target is None:
                unresolved = True
                break
            targets.append(target)
        if unresolved or not targets:
            continue
        if all(str(by_rel[target]["category"]) == "volatile_only" for target in targets):
            row["category"] = "volatile_only"
            row["normalized_equal"] = True
            row["first_diff"] = None
            row["derived_digest_of"] = sorted(set(targets))
            row["note"] = "派生摘要差异：被摘要制品本身仅含白名单易变字段差异"
            reclassified.append(str(row["rel"]))
    return reclassified


def compare_trees(
    before: Path, after: Path, reps_before, reps_after
) -> dict[str, object]:
    before_files, after_files = _collect(before), _collect(after)
    only_before = sorted(set(before_files) - set(after_files))
    only_after = sorted(set(after_files) - set(before_files))
    results: list[dict[str, object]] = []
    for rel in sorted(set(before_files) & set(after_files)):
        results.append(
            _compare_one(
                rel, before_files[rel], after_files[rel], reps_before, reps_after
            )
        )

    reclassified = _apply_digest_closure(results)

    summary = {
        "equal": sum(1 for row in results if row["category"] == "equal"),
        "volatile_only": sum(1 for row in results if row["category"] == "volatile_only"),
        "value": sum(1 for row in results if row["category"] == "value"),
        "structural": len(only_before) + len(only_after),
    }
    if summary["structural"]:
        exit_code = 3
    elif summary["value"]:
        exit_code = 2
    else:
        exit_code = 0
    return {
        "before": str(before),
        "after": str(after),
        "file_counts": {"before": len(before_files), "after": len(after_files)},
        "only_before": only_before,
        "only_after": only_after,
        "summary": summary,
        "results": results,
        "derived_digest_reclassified": reclassified,
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# 反例自检（negative control）
# ---------------------------------------------------------------------------


def _write_fixture(root: Path) -> None:
    """写出一套覆盖全部分派的产物对。

    两侧的**内容**完全一致，只有绝对路径字段引用各自的产物根 (root)，
    因此"基线"考验的正是 N1 前缀归一化。
    """

    import pandas as pd

    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "daily").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "generated_at": "2026-08-20T00:00:00+00:00",
                "report_date": "2026-08-20",
                "as_of": "2026-08-20T00:00:00+00:00",
                "path": f"{root}\\reports\\metrics.json",
                "metrics": {"sharpe": 1.25, "trades": 7, "result_hash": "a" * 64},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "symbol": ["BTC", "XAUUSD"],
            "trades": [3, 4],
            "equity": [100000.0, 100123.5],
        }
    ).to_csv(root / "reports" / "table.csv", index=False)
    pd.DataFrame({"value": [1.0, 2.0, 3.0]}).to_parquet(root / "reports" / "frame.parquet")
    (root / "reports" / "report.html").write_text(
        "<html><body>run=fixed</body></html>", encoding="utf-8"
    )
    # 派生摘要集合：摘要的是 metrics.json 的**原始字节**，因此它会继承
    # metrics.json 中 `path` 字段（N1 白名单）造成的原始字节差异。
    metrics_bytes = (root / "reports" / "metrics.json").read_bytes()
    (root / "reports" / "checksums.json").write_text(
        json.dumps(
            {"reports/metrics.json": "sha256:" + hashlib.sha256(metrics_bytes).hexdigest()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "daily" / "blob.bin").write_bytes(b"\x00\x01payload")


def _run_compare(
    before: Path,
    before_roots: list[Path],
    after: Path,
    after_roots: list[Path],
    tree_rep: Path,
):
    """两侧各自用自己的产物根做 N1 前缀归一化（先具体根、后共同祖先）。"""

    reps_before = [(str(root), "«OUT»") for root in before_roots]
    reps_before.append((str(tree_rep), "«TREE»"))
    reps_after = [(str(root), "«OUT»") for root in after_roots]
    reps_after.append((str(tree_rep), "«TREE»"))
    return compare_trees(before, after, reps_before, reps_after)


def _non_equal_rows(outcome: dict[str, object]) -> list[str]:
    rows = [
        f"[{row['category']}] {row['rel']} :: {row['first_diff']}"
        for row in outcome["results"]
        if row["category"] != "equal"
    ]
    rows += [f"[仅before] {rel}" for rel in outcome["only_before"]]
    rows += [f"[仅after] {rel}" for rel in outcome["only_after"]]
    return rows


def self_test() -> int:
    base = Path(tempfile.mkdtemp(prefix="结构对比自检_"))
    try:
        before_rep, after_rep = base / "out_before", base / "out_after"
        _write_fixture(before_rep)
        _write_fixture(after_rep)

        baseline = _run_compare(before_rep, [before_rep], after_rep, [after_rep], base)
        if baseline["exit_code"] != 0:
            print("[自检][FAIL] 未注入差异时不应报出差异:", baseline["summary"])
            for line in _non_equal_rows(baseline):
                print("  -", line)
            return 1
        print("[自检][OK] 基线（仅路径字段指向各自产物根）归一化后严格相等")

        def mutate_json_numeric(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["metrics"]["sharpe"] = 1.26
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        def mutate_json_volatile_key(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["generated_at"] = "2026-10-20T11:22:33+00:00"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        def mutate_json_ts_value(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["as_of"] = "2026-10-20T11:22:33+00:00"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        def mutate_json_date_value(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["report_date"] = "2026-10-20"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        def mutate_csv_numeric(path: Path) -> None:
            path.write_text(
                path.read_text(encoding="utf-8").replace("100123.5", "100123.6"),
                encoding="utf-8",
            )

        def mutate_csv_rows(path: Path) -> None:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        def mutate_html_text(path: Path) -> None:
            path.write_text(
                path.read_text(encoding="utf-8").replace("run=fixed", "run=tampered"),
                encoding="utf-8",
            )

        def mutate_parquet_numeric(path: Path) -> None:
            import pandas as pd

            frame = pd.read_parquet(path)
            frame.loc[0, "value"] = 9.0
            frame.to_parquet(path)

        def mutate_bytes(path: Path) -> None:
            path.write_bytes(b"\x00\x01payload-tampered")

        def mutate_digest_target_real(path: Path) -> None:
            """篡改数值，并同步更新同侧派生摘要 ⇒ 摘要差异指向真实差异。"""

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["metrics"]["sharpe"] = 9.99
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            checksums = path.parent / "checksums.json"
            checksums.write_text(
                json.dumps(
                    {
                        "reports/metrics.json": "sha256:"
                        + hashlib.sha256(path.read_bytes()).hexdigest()
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        def mutate_missing(path: Path) -> None:
            path.unlink()

        scenarios = [
            ("json 数值被篡改", "value", ("reports/metrics.json", mutate_json_numeric)),
            ("csv 数值被篡改", "value", ("reports/table.csv", mutate_csv_numeric)),
            ("csv 行数被篡改", "value", ("reports/table.csv", mutate_csv_rows)),
            ("html 文本被篡改", "value", ("reports/report.html", mutate_html_text)),
            ("parquet 数值被篡改", "value", ("reports/frame.parquet", mutate_parquet_numeric)),
            ("未知二进制被篡改", "value", ("daily/blob.bin", mutate_bytes)),
            ("文件缺失", "structural", ("reports/table.csv", mutate_missing)),
            ("仅改 generated_at", "volatile_only",
             ("reports/metrics.json", mutate_json_volatile_key)),
            ("仅改时间戳取值（非易变键）", "volatile_only",
             ("reports/metrics.json", mutate_json_ts_value)),
            ("仅改裸日期取值", "volatile_only",
             ("reports/metrics.json", mutate_json_date_value)),
            # 对抗：派生摘要的差异必须被归因；一旦指向真实差异就不得被"洗白"。
            ("派生摘要指向真实差异（不得洗白）", "value",
             ("reports/metrics.json", mutate_digest_target_real)),
        ]

        failures: list[str] = []
        for index, (name, expected, (rel, mutate)) in enumerate(scenarios):
            target = base / f"mut_{index}"
            shutil.copytree(after_rep, target, dirs_exist_ok=True)
            mutate(target / rel)
            outcome = _run_compare(
                before_rep, [before_rep], target, [target, after_rep], base
            )
            probe_rel = "reports/checksums.json" if "派生摘要" in name else rel
            observed = _classify(outcome, probe_rel)
            if observed != expected:
                failures.append(f"{name}: 期望 {expected}，实得 {observed}")
                for line in _non_equal_rows(outcome):
                    failures.append(f"    证据 {line}")
            else:
                print(f"[自检][OK] {name} -> {observed}")

        if failures:
            print("[自检][FAIL] 比较器未能按预期报出差异：")
            for line in failures:
                print("  -", line)
            return 1
        print(f"[自检][PASS] 基线 + {len(scenarios)} 个反例全部按预期判定")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _classify(outcome: dict[str, object], rel: str) -> str:
    if rel in outcome["only_before"] or rel in outcome["only_after"]:
        return "structural"
    for row in outcome["results"]:
        if row["rel"] == rel:
            return str(row["category"])
    return "missing"


# ---------------------------------------------------------------------------
# junit 回归对比（S8）
# ---------------------------------------------------------------------------


def compare_junit(
    before_xml: Path, after_xml: Path, attributed: set[str] | None = None
) -> dict[str, object]:
    """比对两份 pytest junit。

    判定口径（偏离方案 §6 原话，理由见回执 deviations_from_plan）：

    1. **失败用例 id 集合**必须完全相同；
    2. 两态**共有**用例的结果必须逐个相同（禁止 pass→fail 回归）；
    3. 仅存在于一侧的用例不得包含失败（P0 新增的 3 个边界断言必须全绿）；
    4. 失败用例的**错误类型/消息**若不同，必须由调用方通过
       `--attributed-failure-type-diff <test_id>` 显式声明归因；否则判 FAIL。

    第 4 条是刻意的：环境态差异（例如改前工作树缺少未入库的
    `data/metadata/研究目录_v3.sqlite3`）会让同一用例以不同异常失败，
    这种差异不能靠"放宽比较"掩盖，必须点名并给出归因。
    """

    attributed = attributed or set()

    def parse(path: Path) -> dict[str, object]:
        root = ET.parse(path).getroot()
        # pytest 产出 <testsuites>，真正的计数在内层 <testsuite> 上；
        # 直接读根节点属性会得到 0 个失败（把失败误判为通过）。
        suites = list(root.iter("testsuite"))

        def total(key: str) -> int:
            return sum(int(suite.get(key) or 0) for suite in suites)

        outcomes: dict[str, str] = {}
        failures: list[dict[str, str]] = []
        for case in root.iter("testcase"):
            test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
            outcome = "passed"
            detail_type = ""
            for kind in ("failure", "error", "skipped"):
                node = case.find(kind)
                if node is not None:
                    outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[kind]
                    detail_type = (node.get("type") or node.get("message") or "").splitlines()[0]
                    break
            # 同名用例若重复出现，保留更严重的结果。
            rank = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
            if test_id not in outcomes or rank[outcome] > rank[outcomes[test_id]]:
                outcomes[test_id] = outcome
            if outcome in {"failed", "error"}:
                failures.append({"test": test_id, "outcome": outcome, "type": detail_type})
        tests_total = total("tests") or len(outcomes)
        return {
            "tests": tests_total,
            "failures": total("failures"),
            "errors": total("errors"),
            "skipped": total("skipped"),
            "passed": tests_total - total("failures") - total("errors") - total("skipped"),
            "outcomes": outcomes,
            "failure_set": sorted(failures, key=lambda item: item["test"]),
        }

    left, right = parse(before_xml), parse(after_xml)
    left_outcomes, right_outcomes = left["outcomes"], right["outcomes"]
    common = sorted(set(left_outcomes) & set(right_outcomes))
    only_before = sorted(set(left_outcomes) - set(right_outcomes))
    only_after = sorted(set(right_outcomes) - set(left_outcomes))

    changed = [
        {
            "test": test_id,
            "before": left_outcomes[test_id],
            "after": right_outcomes[test_id],
        }
        for test_id in common
        if left_outcomes[test_id] != right_outcomes[test_id]
    ]
    regressions = [
        item for item in changed if item["before"] == "passed" and item["after"] != "passed"
    ]
    new_failures = [
        test_id for test_id in only_after if right_outcomes[test_id] in {"failed", "error"}
    ]

    left_failed = {item["test"]: item for item in left["failure_set"]}
    right_failed = {item["test"]: item for item in right["failure_set"]}
    type_diffs = [
        {
            "test": test_id,
            "before": left_failed[test_id],
            "after": right_failed[test_id],
        }
        for test_id in sorted(set(left_failed) & set(right_failed))
        if (left_failed[test_id]["outcome"], left_failed[test_id]["type"])
        != (right_failed[test_id]["outcome"], right_failed[test_id]["type"])
    ]
    unattributed = [item for item in type_diffs if item["test"] not in attributed]

    failed_id_set_equal = set(left_failed) == set(right_failed)
    exit_code = (
        0
        if failed_id_set_equal
        and not regressions
        and not new_failures
        and not unattributed
        else 2
    )
    return {
        "before": {k: left[k] for k in ("tests", "passed", "failures", "errors", "skipped")},
        "after": {k: right[k] for k in ("tests", "passed", "failures", "errors", "skipped")},
        "before_failure_set": left["failure_set"],
        "after_failure_set": right["failure_set"],
        "failed_test_id_set_identical": failed_id_set_equal,
        "failure_detail_identical": not type_diffs,
        "failure_type_differences": type_diffs,
        "attributed_failure_type_differences": [item for item in type_diffs if item["test"] in attributed],
        "unattributed_failure_type_differences": unattributed,
        "common_case_count": len(common),
        "only_before_cases": only_before,
        "only_after_cases": only_after,
        "common_outcome_changes": changed,
        "common_regressions": regressions,
        "only_after_failures": new_failures,
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--before-tree", default=None)
    parser.add_argument("--after-tree", default=None)
    parser.add_argument("--processed-dir", default=None)
    parser.add_argument("--out", default=".tmp/compare/对比结果.json")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--junit-before", default=None)
    parser.add_argument("--junit-after", default=None)
    parser.add_argument(
        "--attributed-failure-type-diff",
        action="append",
        default=[],
        metavar="TEST_ID",
        help="显式声明某个失败用例的错误类型差异已归因于环境态（可重复）；未声明的差异判 FAIL",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.junit_before or args.junit_after:
        if not (args.junit_before and args.junit_after):
            parser.error("--junit-before 与 --junit-after 必须成对提供")
        report = compare_junit(
            Path(args.junit_before), Path(args.junit_after), set(args.attributed_failure_type_diff)
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"before: {json.dumps(report['before'], ensure_ascii=False)}")
        print(f"after : {json.dumps(report['after'], ensure_ascii=False)}")
        print(f"失败用例 id 集合是否相同: {report['failed_test_id_set_identical']}")
        print(f"共有用例数: {report['common_case_count']}；结果变更: "
              f"{len(report['common_outcome_changes'])}；pass→fail 回归: "
              f"{len(report['common_regressions'])}")
        print(f"仅 after 存在用例: {len(report['only_after_cases'])}（其中失败: "
              f"{len(report['only_after_failures'])}）")
        print(f"仅 before 存在用例: {len(report['only_before_cases'])}")
        for item in report["only_before_cases"]:
            print(f"  [仅before] {item}")
        for item in report["only_after_cases"]:
            print(f"  [仅after] {item}")
        for item in report["common_outcome_changes"]:
            print(f"  [结果变更] {item['test']}: {item['before']} -> {item['after']}")
        for item in report["failure_type_differences"]:
            tag = "已归因" if item["test"] in set(args.attributed_failure_type_diff) else "未归因"
            print(f"  [错误类型差异/{tag}] {item['test']}")
            print(f"      before: {item['before']['type']}")
            print(f"      after : {item['after']['type']}")
        print(f"退出码: {report['exit_code']}  （报告: {out}）")
        return int(report["exit_code"])

    if not (args.before and args.after):
        parser.error("必须提供 --before 与 --after（或使用 --self-test / --junit-*）")

    before, after = Path(args.before).resolve(), Path(args.after).resolve()
    shared = [p for p in (args.processed_dir,) if p]
    reps_before = [(str(before), "«OUT»")] + [(str(Path(p).resolve()), "«IN»") for p in shared]
    reps_after = [(str(after), "«OUT»")] + [(str(Path(p).resolve()), "«IN»") for p in shared]
    if args.before_tree:
        reps_before.append((str(Path(args.before_tree).resolve()), "«TREE»"))
    if args.after_tree:
        reps_after.append((str(Path(args.after_tree).resolve()), "«TREE»"))

    report = compare_trees(before, after, reps_before, reps_after)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"before 文件数: {report['file_counts']['before']}")
    print(f"after  文件数: {report['file_counts']['after']}")
    print(f"汇总: {json.dumps(report['summary'], ensure_ascii=False)}")
    for row in report["results"]:
        if row["category"] != "equal":
            print(
                f"  [{row['category']}] {row['rel']} :: {row['first_diff']}"
            )
    for rel in report["only_before"]:
        print(f"  [仅before] {rel}")
    for rel in report["only_after"]:
        print(f"  [仅after] {rel}")
    print(f"退出码: {report['exit_code']}  （报告: {out}）")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
