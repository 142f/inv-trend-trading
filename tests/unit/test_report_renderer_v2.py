from __future__ import annotations

from inv_trend.application.daily_models import (
    BreakoutAssessment,
    InstrumentReportBundle,
    MarketAssessment,
    TurtleObservation,
)
from inv_trend.observability.report_renderer import (
    _JS,
    render_daily_dashboard,
)


def _bundle() -> InstrumentReportBundle:
    assessment = BreakoutAssessment(
        assessment_id="breakout-1",
        signal_id="signal-1",
        timestamp="2026-08-20T00:00:00+00:00",
        timeframe="D1",
        current_price=101.0,
        breakout_type="向上突破",
        breakout_object="海龟 20 日唐奇安上轨",
        breakout_level=100.0,
        previous_state="位于通道区间内",
        post_state="位于上轨上方",
        trend="上升趋势",
        trend_basis=("SMA 排列：多头",),
        entry_direction="做多",
        signal_strength="A级 / 7.0 分",
        triggered_conditions=("海龟 20 日多头价格突破",),
        unmet_conditions=(),
        quality_notes=("相对成交量未确认（非硬门槛）",),
        conclusion="向上突破与 A 级多头共振同向，展示性入场判断为做多。",
    )
    return InstrumentReportBundle(
        symbol="BTC",
        instrument_id="BTCUSDT.BINANCE.SPOT",
        timeframe="D1",
        dataset_version="renderer-test-v2",
        generated_at="2026-08-21T19:00:00+08:00",
        latest_bar={"timestamp": "2026-08-20T00:00:00+00:00", "close": 101.0},
        series=(
            {
                "timestamp": "2026-08-20T00:00:00+00:00",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
            },
        ),
        signals=(),
        rule_evaluations=(),
        market_assessment=MarketAssessment(
            as_of="2026-08-20T00:00:00+00:00",
            market_status="已使用最新完整 D1 K 线",
            trend="上升趋势",
            trend_basis=("SMA 排列：多头",),
            entry_direction="做多",
            entry_reason="同向 A 级共振。",
            rating_grade="A",
            rating_score=7.0,
        ),
        breakout_assessments=(assessment,),
        turtle_observations=(
            TurtleObservation(
                observation_id="observation-1",
                timestamp="2026-08-20T00:00:00+00:00",
                period=20,
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                channel_high=100.0,
                channel_low=90.0,
                intraday_directions=("up",),
                close_confirmation="up",
                status="close_confirmed_up",
                upper_excess_pct=0.02,
                lower_excess_pct=0.0,
            ),
        ),
        summary={"signals": 1, "rules": 0, "anomalies": 0},
    )


def test_renderer_is_chinese_and_uses_structured_breakout_data() -> None:
    bundle = _bundle()
    html = render_daily_dashboard(
        {
            "report_date": "2026-08-21",
            "symbols": [{"symbol": "BTC", "run_status": "updated", "report_bundle": bundle.to_dict()}],
        }
    )

    for text in (
        "趋势策略每日分析报告",
        "市场状态",
        "趋势判断",
        "正式收盘确认突破",
        "盘中越轨观察",
        "可视窗口",
        "显示全部盘中观察",
        "入场方向",
        "触发依据",
        "未满足条件",
        "海龟 20 日唐奇安上轨",
        "HTML 只做中文展示和联动，不重新计算交易逻辑",
    ):
        assert text in html
    assert "Strategy checks" not in html
    assert '<script src="http' not in html
    assert bundle.result_hash in html


def test_v3_bundle_without_additive_report_fields_remains_renderable() -> None:
    payload = _bundle().to_dict()
    payload["schema_version"] = "3"
    payload.pop("turtle_observations")
    payload.pop("anomaly_episodes")

    html = render_daily_dashboard(
        {"report_date": "2026-08-21", "symbols": [{"symbol": "BTC", "report_bundle": payload}]}
    )

    assert "当前可视范围没有海龟通道数据" in _JS
    assert "const all=current?.turtle_observations||[]" in _JS
    assert "正式收盘确认突破" in html


def test_dashboard_keeps_failed_symbol_visible() -> None:
    bundle = _bundle().to_dict()
    html = render_daily_dashboard(
        {
            "report_date": "2026-08-21",
            "symbols": [
                {"symbol": "BTC", "run_status": "updated", "report_bundle": bundle},
                {
                    "symbol": "QQQ",
                    "run_status": "failed",
                    "update": {"error": "no current dataset for QQQ/D1"},
                },
            ],
        }
    )

    assert "QQQ" in html
    assert "数据不可用" in html
    assert "无法生成报告" in html


def test_embedded_dashboard_javascript_has_balanced_delimiters() -> None:
    """A syntax error would prevent all embedded snapshot data from rendering."""

    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(_JS):
        if _JS.startswith("/[&<>\"']/g", index):
            # The dashboard escape helper contains a JavaScript regex literal;
            # quote characters inside its character class are not strings.
            index += len("/[&<>\"']/g")
            continue
        char = _JS[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in pairs:
            assert stack and stack.pop() == pairs[char]
        index += 1
    assert quote is None
    assert not stack


def test_dashboard_chart_lifecycle_is_single_mount_and_visibility_aware() -> None:
    """Hidden tabs and repeated reflow requests must not remount chart handlers."""

    assert "function initializeCharts()" in _JS
    assert "if(chartObserversStarted)return" in _JS
    assert "new ResizeObserver" in _JS
    assert "new IntersectionObserver" in _JS
    assert "new MutationObserver" in _JS
    assert "visibilitychange" in _JS
    assert "pageshow" in _JS
    assert "requestAnimationFrame" in _JS
    assert "chartRetryAttempts<8" in _JS
    assert "canvas.onmousemove" not in _JS
    assert "window.addEventListener('resize',()=>renderCharts())" not in _JS


def test_dashboard_chart_model_is_rebuilt_only_when_data_or_range_changes() -> None:
    assert "function rebuildChartModel()" in _JS
    assert "function scheduleChartRender(rebuild=false)" in _JS
    assert "if(rebuild)chartModel=null" in _JS
    assert "scheduleChartRender(true)" in _JS
    assert "scheduleChartRender(false)" in _JS


def test_dashboard_uses_fixed_centerable_chart_window() -> None:
    assert "windowSize=90" in _JS
    assert "function maxChartStart(total)" in _JS
    assert "function chartWindowBounds(total)" in _JS
    assert "function centeredChartStart(index,total)" in _JS
    assert "index-Math.floor(windowSize/2)" in _JS
    assert "series.slice(start,end)" in _JS
    assert "Math.min(startIndex,index)" not in _JS


def test_table_and_chart_navigation_have_distinct_destinations() -> None:
    assert "function focusChartItem(kind,id,origin='table')" in _JS
    assert "document.getElementById('chartPanel')?.scrollIntoView" in _JS
    assert "if(origin==='chart')" in _JS
    assert "focusObservation(hit.id,'chart')" in _JS
    assert "focusAssessment(hit.id,'chart')" in _JS


def test_price_chart_is_candlestick_first_and_limits_observation_noise() -> None:
    price_spec = next(line for line in _JS.splitlines() if "id:'priceChart'" in line)
    assert "'close'" not in price_spec
    assert "strokeRect" in _JS
    assert "row.open" in _JS and "row.high" in _JS and "row.low" in _JS and "row.close" in _JS
    assert "showAllObservations||item.observation_id===activeObservationId" in _JS
    assert "if(active){" in _JS
    assert "data-series" in _JS


def test_turtle_rules_explain_close_confirmation_without_recalculation() -> None:
    assert "收盘确认型海龟 20 日突破" in _JS
    assert "收盘确认型海龟 55 日突破" in _JS
    assert "通道取当前 K 线之前" in _JS
    assert "不含当前 K 线" in _JS
    assert "close &gt; 上轨" in _JS
    assert "high/low 越轨但 close 回到通道内时仅记为观察" in _JS
