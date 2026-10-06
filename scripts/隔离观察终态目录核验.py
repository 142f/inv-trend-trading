"""Verify an isolated observation's Catalog only after recorded workers stop."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.locking import _owner_process_state
from inv_trend.monitoring.扫描器 import CodeVersion
from inv_trend.observability.脱敏 import redact


def terminal_gate(status, tasks, processes, process_state=_owner_process_state):
    issues, checks = [], []
    if status not in {'COMPLETED', 'FAILED'}:
        issues.append('RUN_REPORT_NOT_TERMINAL')
    if not tasks:
        issues.append('NO_TASK_EVIDENCE')
    if any(row.get('finished_at') is None or row.get('status') not in
           {'SUCCESS', 'FAILED', 'INTERRUPTED'} for row in tasks):
        issues.append('TASKS_NOT_TERMINAL')
    if not processes:
        issues.append('NO_SUPERVISOR_EVIDENCE')
    if any(row.get('finished_at') is None for row in processes):
        issues.append('SUPERVISOR_NOT_TERMINAL')
    pids = [row.get('pid') for row in tasks]
    pids.extend(row.get('child_pid') for row in processes)
    if any(type(pid) is not int or pid <= 0 for pid in pids):
        issues.append('PROCESS_ID_UNVERIFIED')
    for pid in sorted({pid for pid in pids if type(pid) is int and pid > 0}):
        alive = process_state({'pid': pid})
        checks.append({'pid': pid, 'os_alive': alive})
        if alive is not False:
            issues.append('PROCESS_ALIVE_OR_UNVERIFIED:' + str(pid))
    return issues, checks


def ledger_view(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('runtime database integrity failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('runtime foreign key check failed')
        return {
            'tasks': [dict(row) for row in db.execute('SELECT * FROM runtime_tasks ORDER BY job_id')],
            'processes': [dict(row) for row in db.execute('SELECT * FROM supervisor_processes ORDER BY run_id')],
            'counts': {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                       for name in ('decisions', 'events', 'outbox', 'notification_attempts')},
        }


def current_rows(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Catalog database integrity failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Catalog foreign key check failed')
        return [dict(row) for row in db.execute('SELECT * FROM current_versions ORDER BY symbol,timeframe')]


def check(directory):
    directory = directory.resolve()
    report_path = directory / '持续验收状态.json'
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    database = directory / '运行/监控事件.sqlite3'
    before = ledger_view(database)
    issues, checks = terminal_gate(report.get('status'), before['tasks'], before['processes'])
    receipt = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'observed_run_status': report.get('status'), 'gate_issues': issues,
        'process_checks': checks, 'catalog_verification_attempted': False,
        'catalog_verified': False, 'overall_goal_complete': False,
        'report_sha256': hashlib.sha256(report_bytes).hexdigest(),
        'scope': 'Stopped owned workers and before/after checks only; not an atomic cross-store audit, unrelated-writer exclusion, or whole monitoring acceptance',
    }
    if issues:
        receipt['status'] = 'REFUSED_NOT_VERIFIABLY_STOPPED'
        return receipt, 2
    if report.get('refresh') is not True or report.get('market_catalog_backend') != 'v3-sqlite':
        raise RuntimeError('only explicit isolated v3 update observations are supported')
    data = Path(report['data_root']).resolve()
    if data == directory or not data.is_relative_to(directory):
        raise RuntimeError('data root must be isolated within the observation directory')
    if CodeVersion.capture().source_hash != report['source_hash']:
        raise RuntimeError('current verifier source differs from observed source')
    catalog = data / 'metadata/研究目录_v3.sqlite3'
    before_current = current_rows(catalog)
    expected = {(row['symbol'], row['timeframe']) for row in before['tasks']
                if row['status'] == 'SUCCESS'}
    if not expected or not expected.issubset({(row['symbol'], row['timeframe']) for row in before_current}):
        raise RuntimeError('successful targets lack Catalog current records')
    spec = importlib.util.spec_from_file_location('terminal_soak_hashes',
                                                  Path(__file__).with_name('持续监控验收.py'))
    soak = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(soak)
    before_files = soak.market_hashes(data)
    repository = HistoricalDataService(data, providers={})
    receipt['catalog_verification_attempted'] = True
    result = repository.verify_catalog()
    if not isinstance(result, dict) or type(result.get('valid_current_records')) is not int:
        raise RuntimeError('Catalog verifier returned unverifiable results')
    if any(value for value in result.values() if isinstance(value, list)):
        raise RuntimeError('Catalog consistency failures: ' + json.dumps(result, ensure_ascii=False))
    if result['valid_current_records'] < len(expected):
        raise RuntimeError('Catalog verified current coverage is incomplete')
    if (current_rows(catalog) != before_current or ledger_view(database) != before
            or soak.market_hashes(data) != before_files or report_path.read_bytes() != report_bytes):
        raise RuntimeError('observation changed during terminal Catalog verification')
    receipt.update(status='PASSED_FINAL_CATALOG_ONLY', catalog_verified=True,
                   source_hash=report['source_hash'], catalog=result,
                   verified_targets=sorted(expected), market_files_checked=len(before_files),
                   market_files_unchanged=True, catalog_current_unchanged=True,
                   runtime_records_unchanged=True, provider_updates=0)
    return receipt, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output already exists; preserve earlier evidence')
    try:
        receipt, code = check(args.directory)
    except Exception as exc:
        receipt = {'status': 'FAILED_FINAL_CATALOG_CHECK', 'catalog_verified': False,
                   'overall_goal_complete': False, 'error': type(exc).__name__ + ': ' + redact(str(exc))}
        code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'status': receipt['status'], 'catalog_verified': receipt['catalog_verified'],
                      'native_exit_code': code}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
