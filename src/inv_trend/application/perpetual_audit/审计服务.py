"""Audit orchestration, reproducibility manifests and honest evidence reports."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import subprocess

import pandas as pd
import yaml

from inv_trend.data.永续历史 import coverage_audit, OKXHistory
from .研究协议 import ExperimentRegistry, candidates, digest, schedule
from .统计验证 import concentration
from .认证数据 import load_certified
from .候选比较 import run_comparison
from .历史资格 import HistoricalEligibility
from .亏损归因 import attribute_trades


def source_hash(root):
    sha = hashlib.sha256()
    for path in sorted((Path(root) / 'src').rglob('*')):
        if path.suffix in {'.py', '.yaml', '.js', '.css'}:
            sha.update(path.relative_to(root).as_posix().encode())
            sha.update(path.read_bytes())
    return sha.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str), encoding='utf-8')


def validate_plan(plan):
    expected = {'initial_equity': 100000., 'warmup_bars': 440, 'train_bars': 730,
                'validation_bars': 180, 'min_folds': 3, 'development_end': '2026-04-20', 'test_end': '2026-09-06'}
    if any(plan.get(k) != v for k, v in expected.items()):
        raise ValueError('frozen protocol changed: register a new protocol version before implementation')
    risk = {'trade_stop_risk': .005, 'symbol_stop_risk': .01, 'portfolio_stop_risk': .02,
            'strategy_max_leverage': 3., 'margin_limit': .8, 'volatility_target': .15,
            'covariance_days': 60, 'stress_history_days': 365, 'stress_quantile': .95,
            'margin_mode': 'isolated'}
    if plan.get('risk') != risk or plan.get('execution', {}).get('live_trading') is not False:
        raise ValueError('unsupported risk protocol or live trading request')
    if plan.get('sample_status') == 'UNSEEN_TEST' and not plan.get('unseen_evidence'):
        raise ValueError('unseen sample requires documented provenance')


def _spot_evidence(root):
    path = Path(root) / '优化验证/修复后/完整回测结果.json'
    if not path.exists():
        return dict(status='MISSING')
    payload = json.loads(path.read_text(encoding='utf-8'))
    combinations = payload.get('combinations', [])
    baseline = next((c for c in combinations if c['parameters'].get('rules.stop_n') == 2 and
                     c['parameters'].get('rules.pyramid_step_n') == .5), None)
    if not baseline:
        return dict(status='MISSING_BASELINE')
    trades = baseline.get('trades', [])
    pnls = [float(t['pnl']) for t in trades]
    full = baseline['metrics']
    return dict(status='SAVED_SPOT_EVIDENCE_NOT_PERPETUAL', source=str(path),
                source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), metrics=full,
                holdout=baseline.get('holdout_metrics', {}), trades=trades,
                concentration=concentration(pnls, initial_equity=100000., ending_equity=full['ending_equity']))


def run_audit(root, *, data_root=None, output=None, download=False, max_pages=None):
    root = Path(root).resolve()
    data_root = Path(data_root) if data_root else root / 'data/永续审计'
    output = Path(output) if output else root / 'outputs/永续策略审计'
    output.mkdir(parents=True, exist_ok=True)
    plan_path = root / 'config/永续策略审计计划.yaml'
    plan = yaml.safe_load(plan_path.read_text(encoding='utf-8'))
    validate_plan(plan)
    registry = ExperimentRegistry(output / '实验登记.jsonl', root=root)
    code = source_hash(root)
    try:
        git_result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
                                    capture_output=True, text=True, check=False)
        git_head = git_result.stdout.strip() if git_result.returncode == 0 else None
    except FileNotFoundError:
        git_head = None  # ZIP发布没有.git；源码内容哈希仍为权威身份。
    frozen = dict(schema_version='perpetual-audit-v1', git_head=git_head, code_sha256=code,
                  plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(), plan=plan,
                  candidates=candidates(), new_test_status=plan['sample_status'],
                  unseen_evidence=plan.get('unseen_evidence'), live_trading=False)
    freeze_path = output / f'实验锁定-{digest(frozen)[:16]}.json'
    if not freeze_path.exists():
        write_json(freeze_path, frozen)
    if download:
        client = OKXHistory(data_root)
        # Daily history first, then execution dependencies. All failures persist.
        for stream in ('daily', 'minute', 'mark_price', 'funding'):
            for instrument in plan['symbols']:
                run_id = registry.start(dict(operation='DOWNLOAD', instrument=instrument, stream=stream),
                                        code_hash=code, data_hash='PENDING', sample_status=plan['sample_status'], kind='DATA_AUDIT')
                print(f'采集 {instrument} {stream}', flush=True)
                try:
                    _, manifest = client.download(instrument, stream, max_pages=max_pages)
                    registry.finish(run_id, manifest['status'], error=manifest['error'], observations=manifest['observations'])
                except Exception as exc:
                    registry.finish(run_id, 'FAILED', error=f'{type(exc).__name__}: {exc}')
    coverage, data = coverage_audit(data_root, plan['symbols'])
    pd.DataFrame(coverage).to_csv(output / '历史数据覆盖审计.csv', index=False, encoding='utf-8-sig')
    windows, window_status = [], {}
    for symbol, streams in data.items():
        daily = streams['daily']
        if daily.empty:
            window_status[symbol] = 'MISSING_DAILY_HISTORY'
            continue
        try:
            rows, status = schedule(daily.timestamp, track=f'{symbol}:SIGNAL', sample_status=plan['sample_status'], unseen_evidence=plan.get('unseen_evidence'))
            windows.extend(rows)
            window_status[symbol] = status
        except ValueError as exc:
            window_status[symbol] = str(exc)
    columns = ['track', 'stage', 'start', 'end', 'sample_status', 'fold']
    pd.DataFrame(windows, columns=columns).to_csv(output / '训练验证测试区间.csv', index=False, encoding='utf-8-sig')
    spot = _spot_evidence(root)
    daily_spot = {}
    for symbol in ('BTC', 'ETH'):
        path = root / f'processed_data/cleaned/metal_tech_core/{symbol.lower()}usdt_binance_d1_cleaned.csv'
        if path.exists():
            frame = pd.read_csv(path)
            daily_spot[symbol] = frame.set_index(pd.to_datetime(frame.date, utc=True))
    if spot.get('trades'):
        spot['attribution'] = attribute_trades(spot['trades'], daily_spot)
        write_json(output / '现货亏损分层归因.json', spot['attribution'])
    write_json(output / '现货基线与集中度归因.json', spot)
    if spot.get('trades'):
        pd.DataFrame(spot['trades']).to_csv(output / '现货基线逐笔账本.csv', index=False, encoding='utf-8-sig')
    study, certification_error = None, None
    try:
        bundle = load_certified(data_root)
    except (ValueError, OSError, KeyError, IndexError) as exc:
        certification_error = str(exc)
    else:
        study = run_comparison(bundle, registry, output / '合约实验', code,
                               sample_status=plan['sample_status'],
                               unseen_evidence=plan.get('unseen_evidence'),
                               eligibility_provider=HistoricalEligibility(bundle.specs, bundle.provenance.get('eligibility_context', ())))
        write_json(output / '合约前后对比.json', study)
        windows.extend(study['windows'])
        pd.DataFrame(windows, columns=columns).to_csv(output / '训练验证测试区间.csv', index=False, encoding='utf-8-sig')
    # Without verified execution inputs no candidate is allowed to report PnL.
    statuses = []
    if study:
        statuses = study['results']
    else:
        for candidate in candidates():
            run_id = registry.start(candidate, code_hash=code, data_hash=digest(coverage),
                                    sample_status='DEVELOPMENT')
            registry.finish(run_id, 'BLOCKED', reason='EXECUTION_DATA_NOT_CERTIFIED')
            statuses.append(dict(candidate=candidate, status='BLOCKED',
                                 reason='EXECUTION_DATA_NOT_CERTIFIED', metrics=None))
    report = dict(evidence_level='E0', execution_authenticity='NOT_VERIFIED',
                  strategy_improvement='NOT_PROVEN', independent_test='CONTAMINATED_TEST',
                  paper_forward='NOT_STARTED', coverage=coverage, window_status=window_status,
                  windows=windows, candidates=statuses, registry=registry.summary(),
                  selected_trial_rank=None, deflated_sharpe=None,
                  conclusion='缺少经过认证的永续执行数据；不能生成合约前后收益或宣称改善。',
                  code_sha256=code, plan_sha256=frozen['plan_sha256'])
    report.update(certification_error=certification_error, study=study)
    if study:
        report.update(evidence_level=study['evidence_level'],
                      selected_trial_rank=study['selected_trial_rank'],
                      execution_authenticity='INPUT_CERTIFIED_EXECUTION_SIMULATED',
                      strategy_improvement='DEVELOPMENT_SUPPORTED' if study['selected_id'] else 'NOT_PROVEN',
                      conclusion='合约实验已运行，详见逐组合结果；独立测试及纸面证据不自动升级。')
    write_json(output / '完整审计结果.json', report)
    write_json(output / '完整参数结果.json', statuses)
    if not study:
        pd.DataFrame(columns=['timestamp', 'symbol', 'action', 'contracts', 'price', 'fee', 'funding_cashflow']).to_csv(
            output / '永续逐笔账本.csv', index=False, encoding='utf-8-sig')
    _write_reports(output, report, spot)
    return report


def _write_reports(output, report, spot):
    text = '''# 策略重新审计报告

## 本次结论

证据等级 E0。永续行情、标记价格、完整资金费事件及历史规格尚未完成共同覆盖认证。
当前不能生成真实合约 baseline，也不能比较候选净收益；没有选出新策略，没有部署实盘。
每日完整策略已接入历史资格审核适配器，但仍需当时的执行就绪与既有验证证据，不能用今日结果或假审核替代。

## 已实现的研究约束

440 日预热、730/180 日扩展窗口；缺失日历拒绝压缩；至少三个完整验证窗口。
新测试期默认 CONTAMINATED_TEST，不依据日期自动认定未见。
12 个预登记中心候选、仅诊断的单因素邻域、追加式哈希实验登记。
独立的逐仓状态机、资金费事件、ATR 实际止损风险和压力波动预算；
分钟回放的两个盘内顺序场景明确标记为模拟，不是交易所成交复现。
成对块 bootstrap、DSR、赢家集中度和相对 baseline 比较函数已经提供。

## 现货归因（已保存历史证据）

原始现货回测与永续研究严格分开。旧留出期已被查看，不能再次作为独立测试。
下表是已保存结果的读取，不是本轮新永续回测。

'''
    if spot.get('metrics'):
        text += '| 指标 | 全样本 | 已查看留出 |\n|---|---:|---:|\n'
        for label, key in [('总收益', 'total_return'), ('年化收益', 'annualized_return'),
                           ('最大回撤', 'max_drawdown'), ('Sharpe', 'sharpe_ratio'),
                           ('MAR', 'mar_ratio'), ('胜率', 'win_rate'), ('盈亏比', 'payoff_ratio'),
                           ('交易次数', 'trade_count')]:
            text += f"|{label}|{spot['metrics'].get(key)}|{spot.get('holdout', {}).get(key)}|\n"
        text += '\n旧留出盈利幅度和胜率不足以抵销亏损；交易样本较少。信号质量、退出回吐与成本的因果贡献仍需合约对照实验。\n'
        c = spot['concentration']
        text += f"\n最大一笔占毛盈利 {c['top_1_contribution']:.2%}，前三笔 {c['top_3_contribution']:.2%}，前五笔 {c['top_5_contribution']:.2%}。交易盈亏中位数 {c['median_trade']:.2f} USDT。\n"
        text += '\n这种尾部依赖符合趋势策略特征，不能单凭集中度判为无效；新样本能否再捕获趋势仍待验证。\n'
    text += '\n## 实际数据与窗口\n\n'
    text += '详见《历史数据覆盖审计.csv》和《训练验证测试区间.csv》。没有可验证历史时，区间表只有表头；不得填入假设日期作为实测划分。\n'
    text += '\n## 尚未完成的真实验证\n\n合约 baseline、候选参数回测、真实样本外比较和90日纸面执行尚未完成。数据恢复后必须先认证输入，再运行冻结方案；不能将当前 E0 升级为 E1。\n'
    if report.get('study'):
        text = '# 策略重新审计报告\n\n' + report['conclusion'] + '\n\n证据等级：' + report['evidence_level']
        text += '\n\n执行模型：分钟OHLC场景模拟；历史输入认证不等于交易所订单簿、ADL和清算拍卖复现。\n'
        text += '\n## 全部合约实验\n\n```json\n' + json.dumps(report['study'], ensure_ascii=False, indent=2, default=str) + '\n```\n'
    (output / '策略重新审计报告.md').write_text(text, encoding='utf-8')
    methods = '''# 策略规则与经典方法对照

|当前规则|待验证问题|经典方法|实验|预期作用|风险|
|---|---|---|---|---|---|
|20/55突破|震荡假突破|Turtle/Donchian|双系统与快/慢系统|识别周期适用性|慢信号滞后|
|A级评分|同类信息重复|长期均线过滤|原评分与SMA200分别比较|减少冗余|漏掉早期趋势|
|方向判定|逆长期趋势|Time-Series Momentum|365日自身收益过滤|长期方向一致|转折滞后|
|1N仓位|不等于止损风险|ATR Position Sizing|实际止损距离定量|统一风险预算|跳空超额损失|
|固定加仓|风险集中|Turtle金字塔|有/无加仓|检验增量贡献|削弱大趋势收益|
|通道退出|回吐或过早退出|Donchian/ATR Stop|单因素邻域诊断|控制尾部与回吐|频繁止损|
|BTC/ETH|相关性不稳定|Risk Budgeting|绝对敞口压力波动|避免虚假分散|过度减仓|
|市场分层|少数阶段依赖|Regime Detection|趋势/波动/ADX归因|说明适用环境|事后标签偏差|
|仅两个资产|截面过窄|Cross-Sectional Momentum|仅适用性评估|避免过度扩展|无法验证广泛分散化|

保留突破、保护止损、通道退出和严格资格审核。评分、加仓及过滤器尚无证据支持直接删除或替换。
15%只减风险覆盖规则是本项目假设，不等于Moreira–Muir原方法。

研究原文：
- Turtle: https://tradingblox.com/originalturtles/originalturtlerules.htm
- TSM: https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum
- 长期趋势: https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf
- 波动管理: https://www.nber.org/papers/w22208
- 风险平价: https://www.thierry-roncalli.com/download/erc.pdf
- DSR: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
'''
    (output / '策略规则与经典方法对照.md').write_text(methods, encoding='utf-8')
    payload = html.escape(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    markup = '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    markup += '<title>修改前后回测对比</title><style>body{max-width:1000px;margin:40px auto;padding:20px;font:16px/1.7 sans-serif}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}strong{color:#a33}</style>'
    markup += '<h1>修改前后回测对比</h1><p><strong>' + html.escape(report['evidence_level'] + '：' + report['conclusion']) + '</strong></p>'
    markup += '<p>旧现货与新合约基线分开报告；独立测试及纸面证据不自动升级。</p>'
    markup += '<details open><summary>审计报告</summary><pre>' + html.escape(text) + '</pre></details>'
    markup += '<details><summary>全部候选与数据证据</summary><pre>' + payload + '</pre></details></html>'
    (output / '修改前后回测对比.html').write_text(markup, encoding='utf-8')
