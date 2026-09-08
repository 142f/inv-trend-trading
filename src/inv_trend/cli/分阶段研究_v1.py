"""统一研究入口：study / stage / report；阶段交换只使用数据库中的结构化对象。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from inv_trend.core.阶段协议_v1 import RawBar, RunContext, digest, encode_envelope
from inv_trend.core.在线策略_v1 import Candidate, candidate_grid
from inv_trend.core.事件账户_v1 import ExecutionConfig
from inv_trend.application.标准阶段_v1 import stages, run_checked
from inv_trend.application.多方法研究_v1 import run_study, code_hash
from inv_trend.data.时点行情_v1 import PointInTimeRepository, EQUITIES
from inv_trend.storage.阶段运行_v1 import StageRepository
from inv_trend.observability.研究评审_v1 import render_study


def preflight(root: Path) -> dict:
    targets = ["tests/test_标准阶段_v1.py", "tests/test_时点存储_v1.py", "tests/test_architecture_boundaries.py"]
    if not all((root / p).is_file() for p in targets):
        raise FileNotFoundError("定向验收测试缺失；请在交付项目根目录运行，或指定 --project-root")
    with tempfile.TemporaryDirectory(prefix="研究门禁_") as temporary:
        junit = Path(temporary) / "测试回执.xml"
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", *targets, f"--junitxml={junit}"],
                           cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=300)
        suites = ET.parse(junit).getroot() if junit.exists() else None
        counts = {key: sum(int(x.get(key, "0")) for x in suites.findall("testsuite"))
                  if suites is not None else 0 for key in ("tests", "failures", "errors", "skipped")}
        receipt = {"passed": p.returncode == 0 and counts["tests"] > 0,
                   "code_checks": p.returncode == 0, "returncode": p.returncode, **counts,
                   "stdout": p.stdout, "stderr": p.stderr,
                   "receipt_sha256": hashlib.sha256(junit.read_bytes()).hexdigest() if junit.exists() else None,
                   "scope": "new causal Stage path + existing architecture boundaries; NOT full legacy suite"}
    if not receipt["passed"]:
        raise RuntimeError("定向门禁失败，禁止进入实验：\n" + json.dumps(receipt, ensure_ascii=False))
    return receipt



def resolve_configuration(repository, packet, candidate_id=None):
    if packet.context.code_version != code_hash():
        raise ValueError("输入代码版本与当前实现不一致；须使用匹配实现或新建run，不得冒用旧版本")
    candidate = next((c for c in candidate_grid() if digest(c) == packet.context.parameters_hash), None)
    execution = ExecutionConfig()
    states = repository.store.rows("SELECT stage,state_json,state_hash FROM stage_checkpoints_v1 WHERE run_id=? ORDER BY as_of",
                                    (packet.context.run_id,))
    for row in states:
        state = json.loads(row["state_json"])
        if digest(state) != row["state_hash"]:
            raise ValueError("配置检查点完整性校验失败")
        if row["stage"] in ("indicators", "signal"):
            saved = Candidate(**state["engine"]["candidate"])
            if candidate is not None and saved != candidate:
                raise ValueError("同一run存在冲突的候选参数")
            candidate = saved
        elif row["stage"] == "backtest":
            execution = ExecutionConfig(**state["engine"]["config"])
    if candidate is None:
        raise ValueError("无法从不可变输入解析策略配置；禁止采用隐式默认参数")
    if candidate_id is not None and candidate.id != candidate_id:
        raise ValueError("请求参数与上游锁定参数不一致；请创建独立run")
    return candidate, execution


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="command", required=True)
    study = sub.add_parser("study", help="36组参数、三方法、严格滚动验证；先运行定向门禁")
    study.add_argument("--study-id", required=True)
    study.add_argument("--symbols", nargs="+", default=list(EQUITIES), choices=EQUITIES)
    study.add_argument("--dataset-version")
    study.add_argument("--window-limit", type=int)
    study.add_argument("--project-root", type=Path, default=Path.cwd())
    stage = sub.add_parser("stage", help="独立运行一个 Stage；用标准制品ID读取上游")
    stage.add_argument("--stage", required=True, choices=("acquire", "process", "indicators", "signal", "backtest", "evaluate", "report"))
    stage.add_argument("--input-id")
    stage.add_argument("--candidate-id", choices=[c.id for c in candidate_grid()])
    stage.add_argument("--checkpoint-as-of")
    stage.add_argument("--symbol", choices=EQUITIES, default="QQQ")
    stage.add_argument("--before")
    stage.add_argument("--dataset-version")
    stage.add_argument("--run-id")
    verify = sub.add_parser("verify", help="独立复算账本、锁定顺序、哈希、留出集隔离")
    verify.add_argument("--study-id", required=True)
    report = sub.add_parser("report", help="从权威数据库投影HTML，不重新计算策略")
    report.add_argument("--study-id", required=True)
    report.add_argument("--output", type=Path, default=Path("研究评审_v1.html"))
    args = parser.parse_args(argv)
    try:
        if args.command == "study":
            evidence = preflight(args.project_root.resolve())
            result = run_study(args.data, args.study_id, symbols=tuple(args.symbols),
                               dataset_version=args.dataset_version, window_limit=args.window_limit,
                               test_evidence=evidence)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        repository = StageRepository(args.data)
        if args.command == "verify":
            from inv_trend.storage.研究核验_v1 import verify_study
            result = verify_study(args.data, args.study_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        if args.command == "report":
            rows = repository.store.rows("SELECT payload_json FROM stage_reviews_v1 WHERE study_id=? AND review_id='研究汇总'",
                                         (args.study_id,))
            if not rows:
                raise ValueError("研究未完成或汇总不存在，不能生成成功报告")
            result = json.loads(rows[0]["payload_json"])
            html = render_study(result)
            # Register canonical report in the same DB. The explicit file is a derivative export.
            key = f"reports/{args.study_id}/研究评审_v1.html"
            repository.store.put_document(key, html.encode(), kind="stage_report")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(html)
            print(json.dumps({"output": str(args.output), "canonical": key,
                              "production_enabled": False}, ensure_ascii=False))
            return 0
        candidate = next((c for c in candidate_grid() if c.id == args.candidate_id), candidate_grid()[0])
        if args.stage == "acquire":
            if not args.before or not args.run_id:
                parser.error("acquire 必须提供 --before UTC时间 和 --run-id")
            version = args.dataset_version
            if version is None:
                found = repository.store.rows("SELECT dataset_hash FROM market_datasets_v3")
                if len(found) != 1:
                    raise ValueError("必须显式指定 --dataset-version")
                version = found[0]["dataset_hash"]
            context = RunContext(args.run_id, version, "在线三方法_v1", code_hash(), digest(candidate))
            with PointInTimeRepository(args.data, version, allowed_before=args.before) as source:
                bars = source.history(args.symbol, end=args.before, limit=1)
            if not bars:
                raise ValueError("时间水位之前没有已完成行情")
            input = RawBar(**asdict(bars[0]))
        else:
            if not args.input_id:
                parser.error("非 acquire 阶段必须提供 --input-id")
            input = repository.load(args.input_id)
            context = input.context
        execution = ExecutionConfig()
        if args.stage != "acquire":
            candidate, execution = resolve_configuration(repository, input, args.candidate_id)
        chain = stages(context, candidate, execution)
        selected = next(stage for stage in chain if stage.name == args.stage)
        if args.checkpoint_as_of:
            selected.restore(repository.checkpoint(context.run_id, args.stage, args.checkpoint_as_of))
        result = run_checked(selected, input)
        repository.publish([result], [(context.run_id, selected.name, result.as_of, selected.snapshot())])
        print(json.dumps({"artifact_id": result.artifact_id, "stage": result.stage,
                          "quality": asdict(result.quality), "as_of": result.as_of}, ensure_ascii=False))
        return 1 if result.quality.status == "FAIL" else 0
    except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "FAIL", "error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
