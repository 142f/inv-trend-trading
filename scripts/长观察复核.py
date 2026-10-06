"""Read-only checkpoint audit; does not modify or restart the observed runtime."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from inv_trend.data.locking import _owner_process_state
from inv_trend.monitoring.模型 import DecisionSnapshot
from inv_trend.monitoring.扫描器 import CodeVersion
from inv_trend.notifications.队列 import NotificationQueue


def scan_cadence(tasks,maximum_start_gap_seconds=None):
    """Measure scheduled starts, without claiming process uptime or successful scans."""
    if maximum_start_gap_seconds is not None and (
        type(maximum_start_gap_seconds) not in (int,float)
        or not math.isfinite(maximum_start_gap_seconds) or maximum_start_gap_seconds<=0
    ):
        raise ValueError('maximum start gap must be finite and positive')
    groups = {}
    for row in tasks:
        started = datetime.fromisoformat(row['started_at'])
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError('task start must have an explicit timezone')
        groups.setdefault(row['symbol']+':'+row['timeframe'],[]).append(
            (started.astimezone(timezone.utc),row['job_id']))
    targets,gaps = {},[]
    for key,rows in sorted(groups.items()):
        rows.sort()
        intervals = []
        for before,after in zip(rows,rows[1:]):
            seconds = (after[0]-before[0]).total_seconds()
            intervals.append(seconds)
            if maximum_start_gap_seconds is not None and seconds>maximum_start_gap_seconds:
                gaps.append({'target':key,'seconds':seconds,'before_job_id':before[1],
                    'after_job_id':after[1],'before_started_at':before[0].isoformat(),
                    'after_started_at':after[0].isoformat()})
        targets[key] = {'task_starts':len(rows),'intervals_measured':len(intervals),
            'span_seconds':(rows[-1][0]-rows[0][0]).total_seconds(),
            'maximum_start_gap_seconds':max(intervals) if intervals else None}
    sufficient = bool(targets) and all(r['intervals_measured']>0 for r in targets.values())
    return {'scope':'adjacent persisted task starts only; no boundary coverage or process uptime proof',
        'maximum_allowed_start_gap_seconds':maximum_start_gap_seconds,'targets':targets,
        'excessive_gaps':gaps,'sufficient_adjacent_samples':sufficient,
        'observed_cadence_within_limit':None if maximum_start_gap_seconds is None or not sufficient else not gaps,
        'continuous_uptime_proven':False}


def check(directory,output,maximum_start_gap_seconds=None):
    directory,output = directory.resolve(),output.resolve()
    if output.exists(): raise FileExistsError('preserve checkpoint history: '+str(output))
    status_bytes = (directory/'持续验收状态.json').read_bytes()
    status = json.loads(status_bytes)
    refresh = status.get('refresh',False)
    if type(refresh) is not bool: raise RuntimeError('observation refresh must be explicit boolean')
    code = CodeVersion.capture()
    if code.source_hash!=status['source_hash']: raise RuntimeError('observed source differs from current source')
    path = directory/'运行'/'监控事件.sqlite3'
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=15) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if [r[0] for r in db.execute('PRAGMA quick_check')]!=['ok']:
            raise RuntimeError('SQLite quick_check failed')
        if db.execute('PRAGMA foreign_key_check').fetchall(): raise RuntimeError('broken event-store references')
        root = Path(db.execute("SELECT value FROM runtime_metadata WHERE key='data_root'").fetchone()[0])
        counts = {table:dict(db.execute('SELECT '+field+',count(*) FROM '+table+' GROUP BY '+field).fetchall())
            for table,field in [('runtime_tasks','status'),('events','kind'),('outbox','status'),
                ('notification_attempts','status')]}
        event_codes = dict(db.execute('SELECT code,count(*) FROM events GROUP BY code').fetchall())
        processes = [dict(r) for r in db.execute('SELECT * FROM supervisor_processes ORDER BY rowid')]
        unfinished = db.execute('SELECT count(*) FROM runtime_tasks WHERE finished_at IS NULL').fetchone()[0]
        completed = {(r[0],r[1]):r[2] for r in db.execute(
            "SELECT symbol,timeframe,count(*) FROM runtime_tasks WHERE status IN ('SUCCESS','FAILED') "
            'AND finished_at IS NOT NULL GROUP BY symbol,timeframe')}
        successful = {(r[0],r[1]):r[2] for r in db.execute(
            "SELECT symbol,timeframe,count(*) FROM runtime_tasks WHERE status='SUCCESS' "
            'AND finished_at IS NOT NULL GROUP BY symbol,timeframe')}
        decisions = db.execute('SELECT * FROM decisions').fetchall()
        for row in decisions:
            snap = DecisionSnapshot.from_dict(json.loads(row['payload']))
            if snap.decision_id!=row['decision_id'] or snap.symbol!=row['symbol'] or snap.timeframe!=row['timeframe']:
                raise RuntimeError('snapshot row identity mismatch')
            if snap.code_hash!=status['source_hash']:
                raise RuntimeError('decision was produced by a different source version')
        successful_decisions = db.execute('SELECT DISTINCT d.payload FROM decisions d '
            'JOIN runtime_tasks t ON t.decision_id=d.decision_id '
            "WHERE t.status='SUCCESS' AND t.finished_at IS NOT NULL").fetchall()
        advances = {}
        for row in successful_decisions:
            snap = DecisionSnapshot.from_dict(json.loads(row['payload']))
            if refresh:
                if snap.conditions.get('quality_gate') is not True or snap.conditions.get('freshness_gate') is not True:
                    raise RuntimeError('successful public update lacks verified fresh snapshot')
                if status.get('paper_equity') is not None or snap.conditions.get('execution_gate') is not False or snap.state_after.value=='CONFIRMED_ENTRY':
                    raise RuntimeError('unknown-account public update passed execution confirmation')
            advances.setdefault(snap.symbol+':'+snap.timeframe,set()).add(snap.bar_time)
        envelopes = db.execute('SELECT o.*,e.payload AS event_payload,d.payload AS decision_payload '
            'FROM outbox o JOIN events e ON e.event_id=o.event_id '
            'JOIN decisions d ON d.decision_id=e.decision_id').fetchall()
        for row in envelopes:
            NotificationQueue._envelope(row)
            if row['status']=='DELIVERED' and (not row['delivered_at'] or row['lease_token'] is not None):
                raise RuntimeError('delivered record lacks receipt time or retains lease')
        heartbeats = [dict(r) for r in db.execute('SELECT * FROM runtime_heartbeats')]
        cadence = scan_cadence(db.execute(
            'SELECT job_id,symbol,timeframe,started_at FROM runtime_tasks').fetchall(),maximum_start_gap_seconds)
        db.rollback()
    if refresh:
        if not status.get('data_root') or not status.get('protected_data_root'):
            raise RuntimeError('public update lacks explicit root binding')
        protected = Path(status['protected_data_root']).resolve()
        if root.resolve()!=Path(status['data_root']).resolve() or root.resolve().is_relative_to(protected):
            raise RuntimeError('public update root binding or isolation mismatch')
        hash_root = protected
        hash_file = directory/'受保护行情初始哈希.json'
    else:
        hash_root = root
        hash_file = directory/'行情初始哈希.json'
    before = json.loads(hash_file.read_text(encoding='utf-8'))
    changed = [name for name,value in before.items() if not (hash_root/name).is_file()
        or hashlib.sha256((hash_root/name).read_bytes()).hexdigest()!=value]
    selected = {p.relative_to(hash_root).as_posix() for p in hash_root.rglob('*') if p.is_file() and p.suffix.lower() in {'.csv','.parquet'}}
    selected.update(p.relative_to(hash_root).as_posix() for p in (hash_root/'current').rglob('*.json') if p.is_file())
    changed.extend(sorted(selected-set(before)))
    if changed: raise RuntimeError('protected market input files changed during observation')
    audit_bytes = (directory/'本机接收审计.jsonl').read_bytes()
    lines = audit_bytes.splitlines()
    incomplete_tail = bool(audit_bytes and not audit_bytes.endswith(b'\n'))
    if incomplete_tail: lines = lines[:-1]
    audit = [json.loads(line) for line in lines]
    # Receiver audit and DB reads are different instants; compare identity validity,
    # not instantaneous counts while producer and consumer remain active.
    identities = [r['event_id'] for r in audit]
    if any(len(value)!=64 for value in identities): raise RuntimeError('invalid receiver event identity')
    seen = set()
    for row in audit:
        if row['duplicate']!=(row['event_id'] in seen): raise RuntimeError('receiver duplicate audit mismatch')
        seen.add(row['event_id'])
    receipt = {'checked_at':datetime.now(timezone.utc).isoformat(),'source_hash':code.source_hash,
        'observed_status':status['status'],'latest_report_elapsed_seconds':status['elapsed_seconds'],
        'checkpoint_scope':'consistent read-only SQLite snapshot; receiver and market checks are separate observations',
        'counts':counts,'event_codes':event_codes,'verified_decisions':len(decisions),'verified_envelopes':len(envelopes),
        'scan_cadence':cadence,
        'target_coverage':len(completed),'minimum_completed_cycles':min(completed.values(),default=0),
        'refresh':refresh,'successful_cycles':{symbol+':'+frame:value for (symbol,frame),value in successful.items()},
        'closed_bar_advances':{key:max(0,len(bars)-1) for key,bars in advances.items()},
        'market_validation_root':str(hash_root.resolve()),'updated_root_checked_for_immutability':not refresh,
        'catalog_validation_scope':'active updates are allowed; final Catalog validation belongs to runner after owned workers stop' if refresh else 'market files and current pointers immutable',
        'unfinished_tasks':unfinished,'processes':[{**r,'os_alive':_owner_process_state({'pid':r['child_pid']})} for r in processes],
        'heartbeats':heartbeats,'market_files_checked':len(before),'market_files_unchanged':True,
        'receiver_requests':len(identities),'receiver_unique_events':len(set(identities)),
        'receiver_duplicates':len(identities)-len(set(identities)),
        'receiver_incomplete_tail_ignored':incomplete_tail,
        'status_sha256':hashlib.sha256(status_bytes).hexdigest()}
    output.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:receipt[k] for k in ('observed_status','target_coverage','minimum_completed_cycles',
        'unfinished_tasks','verified_decisions','verified_envelopes','receiver_duplicates','counts')},ensure_ascii=False))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--maximum-start-gap-seconds',type=float,
        help='Explicit maximum adjacent task-start gap; reports violations without claiming uptime')
    parser.add_argument('--require-cadence',action='store_true',
        help='Exit 2 when adjacent scan cadence is violated or unverified; does not certify uptime')
    args = parser.parse_args(argv)
    if args.require_cadence and args.maximum_start_gap_seconds is None:
        parser.error('--require-cadence requires --maximum-start-gap-seconds')
    receipt = check(args.directory,args.output,args.maximum_start_gap_seconds)
    if args.require_cadence and receipt['scan_cadence']['observed_cadence_within_limit'] is not True:
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
