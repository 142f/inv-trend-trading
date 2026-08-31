from __future__ import annotations

import pytest

from inv_trend.application.daily_models import (
    DataUpdateResult,
    StrategyScreeningResult,
)
from inv_trend.application.daily_stages import TrendDecisionService
from inv_trend.application.strategy_config import TrendDecisionConfig


def _data(*, observation_only: bool = False, version: str = "dataset-v1") -> DataUpdateResult:
    return DataUpdateResult(
        symbol="BTC",
        instrument_id="BTCUSDT.BINANCE.SPOT",
        timeframe="D1",
        update_status="unchanged",
        dataset_version=version,
        latest_complete_d1="2026-08-20T00:00:00+00:00",
        quality={"statuses": ["CURATED"], "passed": True},
        freshness={"status": "FRESH"},
        lineage={"verified": True, "curated_sha256": "abc"},
        data_readiness="READY",
        observation_only=observation_only,
    )


def _screen(
    data: DataUpdateResult,
    *,
    grade: str,
    direction: str | None,
    breakout: bool = True,
    eligibility: dict[str, object] | None = None,
) -> StrategyScreeningResult:
    candidates = ()
    if breakout and direction in {"long", "short"}:
        candidates = ({"candidate_id": "turtle-event", "direction": direction, "period": 20},)
    return StrategyScreeningResult(
        symbol=data.symbol,
        instrument_id=data.instrument_id,
        timeframe="D1",
        input_data_hash=data.result_hash,
        configuration_hash="config-v1",
        dataset_version=data.dataset_version,
        as_of=data.latest_complete_d1,
        strategy_checks={
            "sma_alignment": {"direction": direction},
            "ema_trend": {"direction": direction},
            "macd_summary": {"direction": direction},
            "trend_quality": {"direction": direction, "confirmed": True},
            "volatility": {"state": "normal"},
            "volume": {"confirmed": True},
            "rating": {
                "grade": grade,
                "direction": direction,
                "score": 7.0 if grade == "A" else 5.0,
                "family_votes": {"turtle": direction, "sma": direction, "ema": direction, "macd": direction},
                "aligned_families": ["turtle", "sma", "ema", "macd"],
                "quality_adjustments": ["adx_dmi_confirmed:+1"],
            },
        },
        turtle_breakouts=candidates,
        eligibility=eligibility or {
            "status": "NOT_EVALUATED", "evaluated": False, "passed": None, "results": []
        },
        screening_status="OBSERVATION_ONLY" if data.observation_only else "READY",
        observation_only=data.observation_only,
    )


def test_decision_truth_table_uses_screening_evidence_only() -> None:
    data = _data()
    service = TrendDecisionService()

    entered = service.decide(
        data,
        _screen(
            data,
            grade="A",
            direction="long",
            eligibility={
                "status": "PASSED",
                "evaluated": True,
                "passed": True,
                "results": [],
            },
        ),
    )
    assert (entered.trend_direction, entered.decision, entered.execution_state) == (
        "LONG", "LONG", "ENTER_LONG"
    )

    b_grade = service.decide(data, _screen(data, grade="B", direction="short"))
    assert (b_grade.trend_direction, b_grade.decision, b_grade.execution_state) == (
        "SHORT", "WAIT", "WAIT"
    )

    no_breakout = service.decide(data, _screen(data, grade="A", direction="long", breakout=False))
    assert (no_breakout.trend_direction, no_breakout.decision, no_breakout.execution_state) == (
        "LONG", "WAIT", "WAIT"
    )

    c_grade = service.decide(data, _screen(data, grade="C", direction="long"))
    assert (c_grade.trend_direction, c_grade.decision, c_grade.execution_state) == (
        "NEUTRAL", "NEUTRAL", "NOT_APPLICABLE"
    )


def test_eligibility_block_only_downgrades_execution_to_wait() -> None:
    data = _data()
    screening = _screen(
        data,
        grade="A",
        direction="short",
        eligibility={
            "status": "BLOCKED",
            "evaluated": True,
            "passed": False,
            "results": [{"direction": "short", "trade_eligible": False, "hard_blocks": ["EVENT_RISK_HIGH"]}],
        },
    )

    result = TrendDecisionService().decide(data, screening)
    assert result.trend_direction == "SHORT"
    assert result.decision == "WAIT"
    assert result.execution_state == "WAIT"
    assert result.risk_blocks == ("EVENT_RISK_HIGH",)


def test_research_mode_is_observation_only_wait() -> None:
    data = _data(observation_only=True)
    result = TrendDecisionService().decide(data, _screen(data, grade="A", direction="long"))

    assert result.decision == "WAIT"
    assert result.execution_state == "WAIT"
    assert result.observation_only is True
    assert "研究模式" in result.conclusion


def test_hash_chain_is_stable_and_stage_inputs_cascade() -> None:
    source = {"statuses": ["CURATED"], "passed": True}
    first = _data()
    second = _data()
    assert first.result_hash == second.result_hash
    source["passed"] = False
    frozen = DataUpdateResult(
        symbol="BTC", instrument_id="BTCUSDT.BINANCE.SPOT", timeframe="D1",
        update_status="unchanged", dataset_version="dataset-v1", latest_complete_d1=first.latest_complete_d1,
        quality=source, freshness={"status": "FRESH"}, lineage={"verified": True}, data_readiness="READY",
    )
    source["passed"] = True
    assert frozen.to_dict()["quality"]["passed"] is False

    screening = _screen(first, grade="A", direction="long")
    decision = TrendDecisionService().decide(first, screening)
    changed = _data(version="dataset-v2")
    changed_screening = _screen(changed, grade="A", direction="long")
    changed_decision = TrendDecisionService().decide(changed, changed_screening)
    assert decision.input_screening_hash == screening.result_hash
    assert screening.input_data_hash == first.result_hash
    assert changed_screening.input_data_hash == changed.result_hash
    assert changed_decision.input_screening_hash != decision.input_screening_hash


def test_trend_decision_config_is_strict() -> None:
    assert TrendDecisionConfig.from_mapping({"execution_grade": "A"}).execution_grade == "A"
    with pytest.raises(ValueError, match="unsupported trend_decision keys"):
        TrendDecisionConfig.from_mapping({"made_up": True})
    with pytest.raises(ValueError, match="execution_grade"):
        TrendDecisionConfig.from_mapping({"execution_grade": "B"})
