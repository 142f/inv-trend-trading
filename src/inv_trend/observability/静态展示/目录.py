"""Catalog for the fixed HTML showcase pages.

Only presentation metadata lives here.  Data-bearing pages are produced by the
snapshot renderer and remain in ``deliverables/`` at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StaticPage:
    id: str
    title: str
    description: str
    path: str
    kind: Literal["asset", "generated"]


PAGES: tuple[StaticPage, ...] = (
    StaticPage(
        "pipeline-board",
        "数据管理流水线任务看板",
        "P0–P5 任务的完成状态、证据与验收说明。",
        "数据管理流水线任务看板_v1.html",
        "asset",
    ),
    StaticPage(
        "audit-report",
        "美股前100指标审计验收汇报",
        "Phase 1 数据口径、审计缺口与回执字段的验收结论。",
        "美股前100指标_审计验收汇报.html",
        "asset",
    ),
    StaticPage(
        "refactor-acceptance-s1-s5",
        "S1–S5 结构修复验收",
        "结构检测五项问题的修复证据、指标与审计结论。",
        "refactor-acceptance-s1-s5/refactor-acceptance-s1-s5.html",
        "asset",
    ),
    StaticPage(
        "us-top100",
        "美股前100指标看板",
        "QQQ+SPY 前100去重标的的日线指标快照，由脚本按运行时数据生成。",
        "美股前100指标看板.html",
        "generated",
    ),
    StaticPage(
        "us-direction-map",
        "美股前100方向偏度总览",
        "全部参与标的 × 8 个方向系统的投票矩阵、偏度得分与市场分布，由脚本按运行时数据生成。",
        "美股前100方向偏度总览.html",
        "generated",
    ),
    StaticPage(
        "us-decision-desk",
        "美股前100投资决策面板",
        "已触发/临近触发/观察三区决策卡：评级、突破触发价与距离、止损位、建议股数、退出通道，由脚本按运行时数据生成。",
        "美股前100投资决策面板.html",
        "generated",
    ),
    StaticPage(
        "us-market-thermometer",
        "美股前100市场温度面板",
        "市场温度分、评级分布、波动状态、突破计数与温度历史，回答当前环境是否适合顺趋势交易。",
        "美股前100市场温度面板.html",
        "generated",
    ),
)

PAGE_BY_ID = {page.id: page for page in PAGES}

GENERATED_FILES = frozenset({
    "美股前100指标看板.html",
    "美股前100方向偏度总览.html",
    "美股前100投资决策面板.html",
    "美股前100市场温度面板.html",
    "美股前100指标数据.json",
    "美股前100指标_回执.json",
    "市场温度历史.json",
})


def get_page(page_id: str) -> StaticPage:
    try:
        return PAGE_BY_ID[page_id]
    except KeyError as exc:
        raise KeyError(f"unknown page id: {page_id}") from exc
