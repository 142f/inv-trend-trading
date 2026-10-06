"""Independent monitoring commands; market access stays behind Repository."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from threading import Event as StopEvent

from inv_trend.adapters.multi_asset.profiles.asset_profiles import build_asset_specs
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.application.backtest.projector import resolve_rules
from inv_trend.config import load_strategy_mapping
from inv_trend.data.api import HistoricalDataService
from inv_trend.data.provider_factory import create_default_providers
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.健康 import HealthRecorder
from inv_trend.monitoring.回放 import HistoricalReplay
from inv_trend.monitoring.扫描器 import CodeVersion, MonitorScanner
from inv_trend.monitoring.策略适配 import ReferenceAccount, TurtleEvaluator
from inv_trend.monitoring.模型 import timeframe
from inv_trend.monitoring.运行配置 import RuntimeConfig, load_runtime_config
from inv_trend.monitoring.运行时 import MonitoringRuntime
from inv_trend.observability.脱敏 import redact


def build_scanner(config: RuntimeConfig, *, purpose='live'):
    providers = create_default_providers(timeout=config.provider_timeout_seconds,
        retries=config.provider_retries, credential_providers=False) if config.refresh else {}
    repository = HistoricalDataService(config.data_root, providers=providers)
    canonical = load_strategy_mapping(config.strategy_config)
    rules = resolve_rules(canonical, {})
    targets = config.targets(repository)
    symbols = sorted({target.symbol for target in targets})
    contracts = canonical['risk'].get('contracts', {})
    overrides = {}
    for symbol in symbols:
        instrument = repository.instrument(symbol)
        configured = contracts.get(instrument.instrument_id, contracts.get(symbol, {}))
        if not isinstance(configured, dict):
            raise ValueError('asset execution contract must be an object: ' + symbol)
        if {'symbol', 'asset_class'} & configured.keys():
            raise ValueError('execution contract cannot override registered data identity: ' + symbol)
        overrides[symbol] = configured
    specs = build_asset_specs(symbols, overrides,
        asset_classes={symbol: repository.instrument(symbol).asset_class for symbol in symbols})
    path = config.runtime_dir / ('监控事件.sqlite3' if purpose == 'live' else '历史回放.sqlite3')
    return MonitorScanner(repository, EventStore(path, purpose=purpose),
                          TurtleEvaluator(MultiAssetTurtleStrategy(specs, rules)))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Runtime YAML; strategy rules remain in shared strategy YAML')
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--runtime-dir', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('scan', 'once', 'serve', 'supervise', 'supervise-notify', 'watchdog', 'replay', 'notify', 'dashboard', 'retire-expectation'):
        child = sub.add_parser(command)
        if command == 'retire-expectation':
            child.add_argument('--component',required=True,choices=('scanner','scheduler','notification'))
            child.add_argument('--scope',required=True)
            child.add_argument('--expected-epoch',required=True,type=int)
            child.add_argument('--actor',required=True)
            child.add_argument('--reason',required=True)
        if command in {'serve','notify'}:
            child.add_argument('--stop-file', type=Path, help='Owned supervisor stop marker')
        if command == 'dashboard':
            child.add_argument('--port', type=int, default=8000)
        if command in {'notify','supervise-notify'}:
            child.add_argument('--webhook-env', required=True,
                               help='Name of an explicitly selected environment variable containing the webhook URL')
        if command == 'notify':
            child.add_argument('--serve', action='store_true', help='Run independent delivery worker continuously')
            child.add_argument('--batch-size', type=int, default=20)
        if command in {'scan', 'once', 'serve'}:
            child.add_argument('--symbols', nargs='+')
            child.add_argument('--timeframes', nargs='+')
            child.add_argument('--paper-equity', type=float,
                               help='Explicit simulated reference; never submits orders')
        if command == 'replay':
            child.add_argument('--symbol', required=True)
            child.add_argument('--timeframe', required=True)
            child.add_argument('--dataset-version', required=True)
            child.add_argument('--compare-backtest', action='store_true')
            child.add_argument('--paper-equity', type=float)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_runtime_config(args.config) if args.config else RuntimeConfig()
        overrides = {name: getattr(args, name) for name in ('data_root', 'runtime_dir')
                     if getattr(args, name) is not None}
        for name in ('symbols', 'timeframes', 'paper_equity'):
            if getattr(args, name, None) is not None:
                overrides[name] = getattr(args, name)
        if args.command in {'scan', 'watchdog', 'replay', 'notify', 'supervise-notify', 'dashboard','retire-expectation'}:
            overrides['refresh'] = False
        if args.command == 'replay':
            overrides.update(symbols=(args.symbol,), timeframes=(args.timeframe,))
        config = replace(config, **overrides)
        if args.command == 'supervise':
            from inv_trend.monitoring.进程监督 import ProcessSupervisor
            with ProcessSupervisor(config) as supervisor:
                return supervisor.serve()
        if args.command == 'supervise-notify':
            from inv_trend.monitoring.进程监督 import ProcessSupervisor
            from inv_trend.notifications.渠道 import WebhookNotifier
            url = os.environ.get(args.webhook_env)
            if not url:
                raise ValueError('explicit webhook environment variable is empty')
            channel = WebhookNotifier(url)
            with ProcessSupervisor(config,webhook_env=args.webhook_env,
                    notification_channel_id=channel.channel_id) as supervisor:
                return supervisor.serve()
        if args.command == 'dashboard':
            from inv_trend.monitoring.看板 import DashboardReader, create_app
            import uvicorn
            if not 1 <= args.port <= 65535:
                raise ValueError('dashboard port must be between 1 and 65535')
            store = EventStore(config.runtime_dir / '监控事件.sqlite3')
            repository, repository_error = None, None
            try:
                repository = HistoricalDataService(config.data_root, providers={})
            except Exception as exc:
                repository_error = type(exc).__name__ + ': ' + redact(str(exc))
            app = create_app(DashboardReader(store, config, repository, repository_error=repository_error))
            uvicorn.run(app, host='127.0.0.1', port=args.port)
            return 0
        if args.command == 'notify':
            from inv_trend.notifications.渠道 import WebhookNotifier
            from inv_trend.notifications.队列 import NotificationPolicy, NotificationWorker
            if not 1 <= args.batch_size <= 1000:
                raise ValueError('notification batch size must be between 1 and 1000')
            url = os.environ.get(args.webhook_env)
            if not url:
                raise ValueError('explicit webhook environment variable is empty')
            channel = WebhookNotifier(url)
            store = EventStore(config.runtime_dir / '监控事件.sqlite3')
            ledger = RuntimeLedger(store, config.data_root)
            health = HealthRecorder(ledger, code_version=CodeVersion.capture(), config_hash=config.config_hash)
            worker = NotificationWorker(store, channel, policy=NotificationPolicy(**config.notifications), health=health,
                                        heartbeat_timeout_seconds=config.heartbeat_timeout_seconds)
            stop = StopEvent()
            while True:
                if args.stop_file and args.stop_file.exists():
                    return 0
                results = []
                for _ in range(args.batch_size):
                    if args.stop_file and args.stop_file.exists():
                        return 0
                    result = worker.run_once()
                    if result is None:
                        break
                    results.append(result)
                print(json.dumps({'command': 'notify', 'channel_id': channel.channel_id,
                                  'results': results}, ensure_ascii=False), flush=True)
                if not args.serve:
                    return 2 if any(item['error'] or not item['accepted'] for item in results) else 0
                deadline = time.monotonic()+config.poll_seconds
                next_pulse = time.monotonic()+config.heartbeat_timeout_seconds/3
                while time.monotonic()<deadline:
                    if args.stop_file and args.stop_file.exists():
                        return 0
                    current = time.monotonic()
                    if current >= next_pulse:
                        ledger.pulse('notification',channel.channel_id,now=datetime.now(timezone.utc),
                            details={'status':'IDLE'})
                        next_pulse = current+config.heartbeat_timeout_seconds/3
                    stop.wait(min(.2,max(0.,deadline-current),max(0.,next_pulse-current)))
        if args.command == 'retire-expectation':
            configured_key = args.scope
            if args.component == 'scanner':
                symbol,separator,frame = args.scope.rpartition(':')
                if not separator or not symbol:
                    raise ValueError('scanner expectation scope is invalid')
                configured_key = symbol.upper()+':'+timeframe(frame)
            if args.component == 'scheduler' or (args.component == 'scanner' and (not config.symbols or configured_key in {
                    symbol+':'+frame for symbol in config.symbols for frame in config.timeframes})):
                raise ValueError('cannot retire a component enabled by runtime configuration')
            store = EventStore(config.runtime_dir / '监控事件.sqlite3')
            ledger = RuntimeLedger(store,config.data_root)
            result = ledger.retire(args.component,args.scope,expected_epoch=args.expected_epoch,
                                  actor=args.actor,reason=args.reason,now=datetime.now(timezone.utc))
            print(json.dumps({'command':args.command,'retirement':result}))
            return 0
        if args.command == 'watchdog':
            # Does not reopen Market Data, call Strategy, or pulse the monitored scheduler.
            store = EventStore(config.runtime_dir / '监控事件.sqlite3')
            ledger = RuntimeLedger(store, config.data_root)
            recorder = HealthRecorder(ledger, code_version=CodeVersion.capture(), config_hash=config.config_hash)
            expectations = {(component, scope): age for component, scope, age in ledger.expected_components()}
            expectations[('scheduler', 'main')] = config.heartbeat_timeout_seconds
            scanner_age = config.interval_seconds + config.scan_timeout_seconds + config.heartbeat_timeout_seconds
            for symbol in config.symbols:
                for frame in config.timeframes:
                    expectations[('scanner', symbol + ':' + frame)] = scanner_age
            for row in ledger.heartbeats():
                if row['component'] in {'scanner', 'notification'} and (row['component'],row['scope']) not in ledger.retired_components():
                    expectations.setdefault((row['component'], row['scope']),
                        scanner_age if row['component'] == 'scanner' else config.heartbeat_timeout_seconds)
            result = recorder.check_heartbeats(
                [(component, scope, age) for (component, scope), age in sorted(expectations.items())],
                clock=lambda: datetime.now(timezone.utc))
            print(json.dumps({'command': 'watchdog', 'checks': [item['snapshot']['reasons'][0] for item in result]}))
            return 2 if any(item['snapshot']['reasons'][0] == 'HEARTBEAT_MISSING' for item in result) else 0
        scanner = build_scanner(config, purpose='replay' if args.command == 'replay' else 'live')
        if args.command == 'replay':
            account = ReferenceAccount('paper', config.paper_equity) if config.paper_equity is not None else None
            result = HistoricalReplay(scanner).run(args.symbol, args.timeframe, args.dataset_version,
                account=None if args.compare_backtest else account, compare_backtest=args.compare_backtest,
                reference_equity=config.paper_equity or 100000.)
            print(json.dumps(result.to_dict(), ensure_ascii=False))
            return 2 if result.comparison and result.comparison['mismatches'] else 0
        with MonitoringRuntime(scanner, config) as runtime:
            if args.command == 'serve':
                runtime.serve(stop_requested=args.stop_file.exists if args.stop_file else None)
                return 0
            results = runtime.run_once()
            summary = [{'target': item['target'], 'status': item['status'], 'job_id': item.get('job_id'),
                        'created': item.get('created'), 'decision_id': item.get('snapshot', {}).get('decision_id'),
                        'state': item.get('snapshot', {}).get('state_after'),
                        'reasons': item.get('snapshot', {}).get('reasons', [])} for item in results]
            print(json.dumps({'command': args.command, 'targets': len(summary), 'results': summary}, ensure_ascii=False))
            return 2 if any(item['status'] != 'SUCCESS' for item in results) else 0
    except KeyboardInterrupt:
        print('monitoring interrupted', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'monitoring failed: {type(exc).__name__}: {redact(str(exc))}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
