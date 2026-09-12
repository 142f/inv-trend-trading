"""Verify sealed artifacts and replay all locked policies with the latest shared engine."""

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from inv_trend.application.EMA统一基准 import prepare, run, score
from inv_trend.core.EMA趋势策略 import Config
from inv_trend.storage.策略版本登记 import digest, encoded, immutable, snapshot, register


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    args = parser.parse_args()
    root = args.study
    payload = json.loads((root / "完整对比结果.json").read_text(encoding="utf-8"))
    protocol = json.loads((root / "冻结实验协议.json").read_text(encoding="utf-8"))
    checks = json.loads((root / "制品校验.json").read_text(encoding="utf-8"))
    for name, expected in checks.items():
        assert digest((root / name).read_bytes()) == expected, name
    with zipfile.ZipFile(protocol["code"]["archive"]) as archive:
        for name, expected in protocol["code"]["files"].items():
            assert digest(archive.read(name)) == expected, name
    registrations = list((root / "策略登记").rglob("*.json"))
    for path in registrations:
        record = json.loads(path.read_text(encoding="utf-8"))
        saved = record.pop("record_hash")
        assert digest(encoded(record)) == saved
        for name, expected in record["results"].items():
            assert digest(Path(name).read_bytes()) == expected
    frames = {}
    for symbol, item in protocol["input"].items():
        path = root / Path(item["path"]).name
        assert digest(path.read_bytes()) == item["sha256"]
        frames[symbol] = pd.read_parquet(path)
    data = prepare(frames)
    training = json.loads((root / "内层验证证据.json").read_text(encoding="utf-8"))
    audit = root.parent / (root.name + "_最新执行器复核")
    audit.mkdir(exist_ok=False)
    code = snapshot(".", audit / "代码快照")
    replays = []
    for item in payload["rounds"]:
        schedule = item["locked_parameters"]
        for w, lock in zip(protocol["windows"], schedule):
            assert lock["trained_through"] < lock["effective_at"]
            old = lock["previous_candidate"]
            new = f"候选{item['round']:02d}"
            expected = (
                new
                if score(training[w["id"]][new]["metrics"])
                > score(training[w["id"]][old]["metrics"])
                else old
            )
            assert lock["candidate_id"] == expected
        s, r = run(
            data,
            Config(),
            protocol["windows"][0]["test_start"],
            protocol["windows"][-1]["test_end"],
            schedule=schedule,
        )
        old_equity = (
            pd.read_csv(
                next(root.glob(f"第{item['round']:02d}*/滚动策略/逐K权益.csv.gz")), index_col=0
            )
            .iloc[:, 0]
            .to_numpy()
        )
        np.testing.assert_allclose(s["equity"].to_numpy(), old_equity, rtol=1e-12, atol=1e-8)
        for key in ("total", "annual", "drawdown", "sharpe", "calmar", "long_pnl", "short_pnl"):
            assert abs(s["metrics"][key] - item["metrics"][key]) < 1e-10
        od = r.orders
        if len(od):
            if "status" in od:
                od = od.loc[od.status.fillna("filled") == "filled"]
            signals = od.loc[od.signal_time.notna()]
            assert (
                pd.to_datetime(signals.time, utc=True)
                >= pd.to_datetime(signals.signal_time, utc=True)
            ).all()
            assert (
                pd.to_datetime(signals.time, utc=True)
                > pd.to_datetime(signals.signal_bar_time, utc=True)
            ).all()
            entries = signals.loc[signals.action.isin(["open", "add"])]
            assert set(entries.fill_phase) == {"open"}
        m = s["metrics"]
        assert abs(sum(m["stages"].values()) - m["total"]) < 1e-8
        assert abs(m["long_pnl"] + m["short_pnl"] - m["total"]) < 1e-8
        replays.append(
            dict(
                round=item["round"],
                maximum_nav_difference=float(np.max(abs(s["equity"].to_numpy() - old_equity))),
                metrics=m,
            )
        )
        print("最新执行器重放一致", item["round"], flush=True)
    result = dict(
        status="PASS",
        sealed_files_verified=len(checks),
        registry_records_verified=len(registrations),
        original_engine_code=protocol["code"]["source_hash"],
        latest_code=code,
        change="部分平仓后保留存续单位的计费时钟；增加参数训练时间校验。全部11条锁定策略路径与原冻结引擎NAV一致。",
        replays=replays,
        tests="80 relevant tests passed before final replay; see testing note",
    )
    immutable(audit / "复核结果.json", result)
    final = payload["final"]
    m = final["metrics"]
    refs = payload["benchmarks"]
    lines = [
        "# 统一基准结论复核",
        "",
        "11轮策略路径已用最新共享执行器完整重放，净值与封存研究一致。数据、源码归档、登记与结果哈希全部核验通过。",
        "",
        "|比较对象|累计收益|最大回撤|Sharpe|EMA收益差|",
        "|---|---:|---:|---:|---:|",
    ]
    for label, q in {**refs, "最终EMA滚动策略": m}.items():
        lines.append(
            f"|{label}|{q['total']:.2%}|{q['drawdown']:.2%}|{q['sharpe']:.3f}|{m['total'] - q['total']:+.2%}|"
        )
    lines += [
        "",
        "## 是否真正更好",
        "",
        f"最终EMA收益为{m['total']:.2%}，年化{m['annual']:.2%}，最大回撤{m['drawdown']:.2%}，Sharpe {m['sharpe']:.3f}，Calmar {m['calmar']:.3f}，平均仓位{m['utilization']:.2%}。",
        f"相对BuyHold累计收益差{m['total'] - refs['BuyHold']['total']:+.2%}，相对当前可运行海龟策略{m['total'] - refs['当前可运行_corrected_v2']['total']:+.2%}。不能把低仓位带来的低回撤解释为已经产生超额收益。",
        f"相对上一轮EMA最终参数统一重测，收益差{m['total'] - refs['历史EMA最终参数']['total']:+.2%}，Sharpe差{m['sharpe'] - refs['历史EMA最终参数']['sharpe']:+.3f}，回撤差{m['drawdown'] - refs['历史EMA最终参数']['drawdown']:+.2%}。收益/风险可能方向不同，不能只选一个指标宣称全面优越。",
        "发布记录没有已批准的生产版本，B6/冻结v4也缺少与配置匹配的可运行代码，故不能回答已战胜正式/B6策略。没有以旧摘要或近似实现替代这项缺失。",
        "",
        "## 改善来自哪里",
        "",
        f"按建仓单位归因：10/20为{m['stages']['1']:.2%}，10/55为{m['stages']['2']:.2%}，20/55为{m['stages']['3']:.2%}；多头{m['long_pnl']:.2%}，空头{m['short_pnl']:.2%}。这些贡献包含期末浮盈亏，信号间相关，不是互相独立的alpha。",
    ]
    for label in (
        "即时确认",
        "确认2日",
        "确认4日",
        "无辅助",
        "MACD",
        "ADX",
        "成交量",
        "移除阶段1",
        "移除阶段2",
        "移除阶段3",
    ):
        q = final["ablations"][label]["metrics"]
        lines.append(
            f"- {label}：相对最终路径收益变化{q['total'] - m['total']:+.2%}、回撤变化{q['drawdown'] - m['drawdown']:+.2%}、Sharpe变化{q['sharpe'] - m['sharpe']:+.3f}。"
        )
    lines += [
        "",
        "即时/2/4日确认均为解释性消融，主策略始终使用3日。更好或更差的消融没有被拿来回头重选外层参数。辅助指标每次只启用一种；最后每个窗口的参数见下表。",
        "",
        "|窗口|候选|阶段权重|ATR风险预算|止损倍数|辅助|空头系数|",
        "|---|---|---|---:|---:|---|---:|",
    ]
    for lock in final["locked_parameters"]:
        p = lock["parameters"]
        lines.append(
            f"|{lock['effective_at']}|{lock['candidate_id']}|{p['weights']}|{p['risk']:.2%}|{p['stop_atr']}|{p['quality']}|{p['short_scale']}|"
        )
    lines += [
        "",
        "没有一组参数在所有窗口稳定胜出。上述风险预算按4个资产分配，仅用于新增单位；三阶段先后进入，共同名义上限总90%/单资产22.5%，不得把阶段比例理解为固定账户满仓比例。",
        "",
        "## 未知未来与版本结论",
        "",
        "外层逐K决策没有读取未来价格，内层选择仅使用7天隔离期之前的历史；但所有外层历史都已在此前研究中查看。因此时间因果性通过，不等于真实未见数据上的泛化能力已经成立。本次未升级正式策略。",
        "",
        "原研究输出不可覆盖；本次复核新增独立目录与代码版本，未修改封存研究。正式B6比较和真正新数据的前瞻验证仍未完成。",
    ]
    (audit / "结论复核.md").write_text("\n".join(lines), encoding="utf-8")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    immutable(
        audit / "未来验证锁定.json",
        dict(
            status="FROZEN_NOT_STARTED",
            production_enabled=False,
            not_before=tomorrow,
            minimum_calendar_days=180,
            assets=protocol["assets"],
            parameters=final["locked_parameters"][-1]["parameters"],
            code_hash=code["source_hash"],
            common_backtest_protocol=str(root / "冻结实验协议.json"),
            selection="use last already-locked window parameters; no future reoptimization",
            evaluation="compare BuyHold, corrected-v2 and registered historical EMA on the same newly collected four-asset data",
            unavailable_baseline="B6 requires matching source; no claim until supplied",
        ),
    )
    register(
        audit / "策略登记",
        strategy_id="EMA统一执行器复核",
        strategy_version="3.0",
        previous=protocol["code"]["source_hash"],
        parameters=final["locked_parameters"],
        context=dict(code=code, original_study=str(root)),
        summary=m,
        differences=result["change"],
        results=[audit / "复核结果.json", audit / "结论复核.md", audit / "未来验证锁定.json"],
    )
    immutable(
        audit / "制品校验.json",
        {
            p.relative_to(audit).as_posix(): digest(p.read_bytes())
            for p in audit.rglob("*")
            if p.is_file()
        },
    )
    print("全部核验通过", audit, flush=True)


if __name__ == "__main__":
    main()
