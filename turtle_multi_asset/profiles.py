"""常用海龟规则配置。"""

from __future__ import annotations

from .strategy import TurtleRules


DEFAULT_CLUSTER_1N_RISK_PCT = {
    "precious_metals": 0.025,
    "crypto": 0.015,
    "us_equity": 0.035,
    "other": 0.02,
}

DEFAULT_CLUSTER_LEVERAGE = {
    "precious_metals": 1.0,
    "crypto": 0.5,
    "us_equity": 1.0,
    "other": 0.5,
}


def turtle_rules(
    profile: str,
    allow_short: bool = True,
) -> TurtleRules:
    """按配置名称返回规则集。"""

    if profile == "h4-daily-equivalent":
        return h4_daily_equivalent_rules(
            allow_short=allow_short,
        )
    if profile == "classic-bars":
        return classic_bar_rules(
            allow_short=allow_short,
        )
    raise ValueError(f"unsupported rule profile: {profile}")


def classic_bar_rules(
    allow_short: bool = True,
) -> TurtleRules:
    """直接把 20/55/10/20 应用于当前 K 线周期。"""

    return _rules_for_bar_scale(
        bars_per_day=1,
        allow_short=allow_short,
    )


def h4_daily_equivalent_rules(
    allow_short: bool = True,
) -> TurtleRules:
    """把日线经典窗口按 1 天 6 根 H4 K 线近似缩放。"""

    return _rules_for_bar_scale(
        bars_per_day=6,
        allow_short=allow_short,
    )


def _rules_for_bar_scale(
    bars_per_day: int,
    allow_short: bool,
) -> TurtleRules:
    return TurtleRules(
        n_period=20 * bars_per_day,
        fast_entry=20 * bars_per_day,
        slow_entry=55 * bars_per_day,
        fast_exit=10 * bars_per_day,
        slow_exit=20 * bars_per_day,
        stop_n=2.0,
        pyramid_step_n=0.5,
        trigger_mode="close",
        allow_short=allow_short,
        max_total_1n_risk_pct=0.08,
        max_direction_1n_risk_pct=0.06,
        cluster_1n_risk_pct=DEFAULT_CLUSTER_1N_RISK_PCT,
        max_total_leverage=1.5,
        max_direction_leverage=1.2,
        cluster_leverage=DEFAULT_CLUSTER_LEVERAGE,
    )
