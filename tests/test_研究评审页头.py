"""The research review page must expose the same auditable header fields."""
from inv_trend.observability.研究评审_v1 import render_study


def test_study_page_exposes_meta_header():
    report = {
        "study_id": "本地滚动验证_v4",
        "protocol_hash": "a" * 64,
        "distinct_parameters": 36,
        "training_trials": 10,
        "oos_symbol_windows": 8,
        "families": {
            "momentum": {
                "metrics": {"total_return": 0.1, "annualized_return": 0.05,
                            "max_drawdown": 0.02, "sharpe_ratio": 1.2, "fill_count": 12},
                "oos_positive_window_ratio": 0.5,
                "unweighted_mean": 3.0,
                "scores_0_to_5": {"收益": 4.0, "稳定性": 3.0},
            }
        },
        "buy_hold_reference": {"total_return": 0.2, "max_drawdown": 0.1},
    }
    document = render_study(report)
    for text in ("研究编号", "本地滚动验证_v4", "协议 SHA256", "a" * 64,
                 "独立参数", "36 组", "训练折回放", "10 次", "样本外标的窗口", "8 个"):
        assert text in document
    assert '<dl class="meta">' in document and "RESEARCH_ONLY" in document
