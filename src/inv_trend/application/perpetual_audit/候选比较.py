"""Frozen-center experiment execution. Holdout never chooses a replacement."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .分钟执行 import MinuteReplay
from .研究协议 import candidates, neighborhood, schedule, digest
from .统计验证 import metrics, paired_bootstrap, deflated_sharpe, candidate_decision, concentration
from .亏损归因 import trade_statistics
from .证据等级 import evidence_level


def _serialize_result(result, directory):
    directory.mkdir(parents=True, exist_ok=True)
    result['equity'].rename('equity').to_csv(directory / '权益曲线.csv', encoding='utf-8-sig')
    for key, filename in [('trades', '逐笔交易.csv'), ('events', '执行事件.csv'), ('signals', '信号证据.csv'), ('ambiguities', '盘内歧义.csv')]:
        pd.DataFrame(result[key]).to_csv(directory / filename, index=False, encoding='utf-8-sig')


def run_comparison(bundle, registry, output, code_hash, *, eligibility_provider=None,
                   sample_status='CONTAMINATED_TEST', unseen_evidence=None):
    output = Path(output)
    common_daily = bundle.daily['BTC-USDT-SWAP'].index.intersection(bundle.daily['ETH-USDT-SWAP'].index)
    windows, validation_status = schedule(common_daily, bundle.start, track='EXECUTION',
                                         sample_status=sample_status, unseen_evidence=unseen_evidence)
    windows = [w for w in windows if pd.Timestamp(w['end'], tz='UTC') <= bundle.end.normalize()]
    folds = [w for w in windows if w['stage'] == 'VALIDATION']
    results, returns_by_id = [], {}

    def execute(spec, start, end, scenario='worst_case', role='CENTER', risk_mode='standardized', cost_multiplier=1.):
        experiment = dict(spec, start=start, end=end, scenario=scenario, role=role, risk_mode=risk_mode, cost_multiplier=cost_multiplier)
        run_id = registry.start(experiment, code_hash=code_hash, data_hash=bundle.data_hash,
                                sample_status=sample_status if role == 'TEST' else 'DEVELOPMENT')
        try:
            from dataclasses import replace
            specs = [replace(s, fee_rate=s.fee_rate * cost_multiplier) for s in bundle.specs]
            replay = MinuteReplay(bundle.daily, bundle.minute, bundle.marks, bundle.funding,
                                  specs, spec, start=pd.Timestamp(start, tz='UTC'),
                                  end=pd.Timestamp(end, tz='UTC') + pd.Timedelta(hours=23, minutes=59),
                                  scenario=scenario, eligibility_provider=eligibility_provider, risk_mode=risk_mode,
                                  slippage_bps=5. * cost_multiplier).run()
            _serialize_result(replay, output / run_id)
            registry.finish(run_id, 'COMPLETED', result_directory=run_id,
                            result_hash=digest(replay['equity'].tolist()))
            return replay
        except Exception as exc:
            registry.finish(run_id, 'FAILED', error=f'{type(exc).__name__}: {exc}')
            raise

    baseline = next(c for c in candidates() if c['architecture'] == 'dual' and c['pyramid'])
    ordered = [baseline] + [c for c in candidates() if c != baseline]
    for spec in ordered:
        cid = digest(spec)[:16]
        row = dict(id=cid, candidate=spec, status='PENDING', evidence_level='E0')
        results.append(row)
        if not folds:
            run_id = registry.start(spec, code_hash=code_hash, data_hash=bundle.data_hash, sample_status='DEVELOPMENT')
            registry.finish(run_id, 'BLOCKED', reason='INSUFFICIENT_FOLDS')
            row.update(status='BLOCKED', reason='INSUFFICIENT_FOLDS')
            continue
        try:
            runs = [execute(spec, w['start'], w['end']) for w in folds]
            returns = [r['equity'].pct_change().iloc[1:] for r in runs]
            returns_by_id[cid] = returns
            pnls = [t['pnl'] for r in runs for t in r['trades']]
            row.update(status='COMPLETED', fold_metrics=[metrics(r) for r in returns],
                       trade_count=len(pnls), liquidation_count=sum(r['liquidation_count'] for r in runs))
            row['trade_metrics'] = trade_statistics([t for r in runs for t in r['trades']])
            row['turnover'] = float(np.mean([sum(e.get('notional', 0.) for e in r['events']) / r['equity'].mean() for r in runs]))
            if cid != digest(baseline)[:16]:
                row['comparison'] = {str(block): paired_bootstrap(returns_by_id[digest(baseline)[:16]], returns, block=block)
                                     for block in (30, 15, 60)}
                level, reason = candidate_decision(returns, trades=len(pnls), liquidations=row['liquidation_count'],
                                                    comparison=row['comparison']['30'], complete_execution=bundle.status == 'VERIFIED_INPUTS')
                row.update(evidence_level=level, reason=reason)
            row['concentration'] = concentration(pnls, initial_equity=100000. * len(runs),
                                                 ending_equity=sum(r['equity'].iloc[-1] for r in runs))
            # Training and continuous development results remain separate artifacts.
            for w in windows:
                if w['stage'] == 'TRAIN':
                    execute(spec, w['start'], w['end'], role='TRAIN')
            full_end = min(pd.Timestamp('2026-04-20', tz='UTC'), bundle.end.normalize()).date().isoformat()
            full_start = bundle.start.date().isoformat()
            worst = execute(spec, full_start, full_end, role='CONTINUOUS_DEVELOPMENT')
            best = execute(spec, full_start, full_end, scenario='best_case', role='CONTINUOUS_DEVELOPMENT')
            row['continuous_metrics'] = metrics(worst['equity'].pct_change().iloc[1:])
            stress = execute(spec, full_start, full_end, role='DOUBLE_COST', cost_multiplier=2.)
            row['double_cost_metrics'] = metrics(stress['equity'].pct_change().iloc[1:])
            row['ambiguity_count'] = len(worst['ambiguities'])
            row['ambiguity_pnl_range'] = sorted([float(worst['equity'].iloc[-1] - 100000), float(best['equity'].iloc[-1] - 100000)])
            row['ambiguity_range_status'] = 'TWO_SCENARIOS_NOT_EXHAUSTIVE_BOUNDS'
        except Exception as exc:
            row.update(status='FAILED', reason=f'{type(exc).__name__}: {exc}', evidence_level='E0')
    # Original-risk baseline is labeled separately and never enters candidate selection.
    original = dict(status='BLOCKED')
    if folds:
        try:
            r = execute(baseline, bundle.start.date().isoformat(), min(pd.Timestamp('2026-04-20', tz='UTC'), bundle.end.normalize()).date().isoformat(),
                        role='ORIGINAL_RISK_BASELINE', risk_mode='original')
            original = dict(status='COMPLETED', metrics=metrics(r['equity'].pct_change().iloc[1:]))
        except Exception as exc:
            original = dict(status='FAILED', reason=str(exc))
    valid = [row for row in results if row['status'] == 'COMPLETED' and row['id'] in returns_by_id]
    trial_sharpes = [float(np.concatenate(returns_by_id[row['id']]).mean() /
                           np.concatenate(returns_by_id[row['id']]).std(ddof=1)) for row in valid
                     if np.concatenate(returns_by_id[row['id']]).std(ddof=1) > 0]
    variance = float(np.var(trial_sharpes, ddof=1)) if len(trial_sharpes) > 1 else None
    for row in valid:
        row['dsr'] = (deflated_sharpe(np.concatenate(returns_by_id[row['id']]),
                                     trials=max(len(trial_sharpes), registry.summary()['statistical_trials']),
                                     sharpe_variance=variance) if variance is not None else None)
    eligible = [row for row in valid if row['evidence_level'] == 'E1']
    eligible.sort(key=lambda row: (-np.median([m['sharpe'] for m in row['fold_metrics']]),
                                   max(m['max_drawdown'] for m in row['fold_metrics']), row['turnover'],
                                   row['candidate']['architecture'] in ('ma', 'momentum', 'daily'), row['id']))
    selected = eligible[0] if eligible else None
    test_result, diagnostic = None, []
    if selected:
        (output / '冻结候选.json').write_text(json.dumps(dict(candidate=selected['candidate'],
            data_hash=bundle.data_hash, code_hash=code_hash), ensure_ascii=False, indent=2), encoding='utf-8')
        for neighbor in neighborhood(selected['candidate']):
            try:
                runs = [execute(neighbor, w['start'], w['end'], role='DIAGNOSTIC_ONLY') for w in folds]
                diagnostic.append(dict(candidate=neighbor, status='COMPLETED', metrics=[metrics(r['equity'].pct_change().iloc[1:]) for r in runs]))
            except Exception as exc:
                diagnostic.append(dict(candidate=neighbor, status='FAILED', error=str(exc)))
        tests = [w for w in windows if w['stage'] == 'TEST']
        if tests:
            w = tests[0]
            try:
                a = execute(baseline, w['start'], w['end'], role='TEST')
                b = execute(selected['candidate'], w['start'], w['end'], role='TEST')
                test_result = dict(sample_status=sample_status,
                                   baseline=metrics(a['equity'].pct_change().iloc[1:]),
                                   candidate=metrics(b['equity'].pct_change().iloc[1:]),
                                   status='DESCRIPTIVE_ONLY_PENDING_SUFFICIENT_INDEPENDENT_EVIDENCE')
                paired = paired_bootstrap([a['equity'].pct_change().iloc[1:]], [b['equity'].pct_change().iloc[1:]])
                test_result.update(comparison=paired, trade_count=len(b['trades']), liquidations=b['liquidation_count'],
                                   provenance=unseen_evidence,
                                   max_drawdown=test_result['candidate']['max_drawdown'], net_return=test_result['candidate']['total_return'])
                if paired.get('status') == 'READY':
                    test_result.update(sharpe_delta=paired['sharpe']['delta'], sharpe_delta_ci_lower=paired['sharpe']['lower'],
                                       calmar_delta=paired['calmar']['delta'], max_drawdown_delta=paired['max_drawdown']['delta'])
            except Exception as exc:
                test_result = dict(status='FAILED', reason=str(exc))
    level, level_reason = evidence_level(execution_complete=bundle.status == 'VERIFIED_INPUTS',
                                         development_supported=selected is not None, test=test_result)
    ranking = sorted(valid, key=lambda row: -np.median([m['sharpe'] if m['sharpe'] is not None else -np.inf for m in row['fold_metrics']]))
    return dict(results=results, original_baseline=original, windows=windows,
                validation_status=validation_status, selected_id=selected['id'] if selected else None,
                selected_trial_rank=next((i + 1 for i, row in enumerate(ranking) if selected and row['id'] == selected['id']), None),
                evidence_level=level, evidence_reason=level_reason, test=test_result, diagnostics=diagnostic,
                registry=registry.summary())
