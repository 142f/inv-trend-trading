"""Evidence ladder; elapsed time or a positive point estimate cannot promote it."""
from __future__ import annotations

import pandas as pd


def evidence_level(*, execution_complete, development_supported, test=None, paper=None, risk_audit=None):
    if not execution_complete:
        return 'E0', 'EXECUTION_EVIDENCE_INCOMPLETE'
    if not development_supported:
        return 'E0', 'IMPROVEMENT_NOT_PROVEN'
    level = 'E1'
    if not test or test.get('sample_status') != 'UNSEEN_TEST' or not test.get('provenance'):
        return level, 'INDEPENDENT_TEST_NOT_VERIFIED'
    if (test.get('trade_count', 0) < 30 or test.get('liquidations', 0) or
            test.get('max_drawdown', float('inf')) > .2 or
            test.get('net_return', -1) <= 0 or test.get('sharpe_delta', -1) < .1 or
            test.get('sharpe_delta_ci_lower', -1) <= 0 or
            test.get('calmar_delta', -1) < 0 or test.get('max_drawdown_delta', 1) > 0):
        return level, 'INDEPENDENT_SUPPORT_INSUFFICIENT'
    level = 'E2'
    if not paper or paper.get('sample_status') != 'PAPER_FORWARD' or not paper.get('live_record_provenance'):
        return level, 'PAPER_NOT_VERIFIED'
    start, end = pd.Timestamp(paper['started_at']), pd.Timestamp(paper['last_observation_at'])
    if (end - start < pd.Timedelta(days=90) or paper.get('trade_count', 0) < 30 or
            paper.get('unexplained_reconciliation_errors', 1) != 0 or
            paper.get('risk_violations', 1) != 0 or not paper.get('execution_quality_passed') or
            not paper.get('statistical_support')):
        return level, 'PAPER_SUPPORT_INSUFFICIENT'
    level = 'E3'
    if (risk_audit and risk_audit.get('passed') is True and risk_audit.get('source') and
            pd.Timestamp(risk_audit['completed_at']) >= end):
        return 'E4', 'SMALL_LIVE_REVIEW_ONLY_NO_DEPLOYMENT_AUTHORIZATION'
    return level, 'FRESH_RISK_AUDIT_REQUIRED'
