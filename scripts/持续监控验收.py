"""Bounded read-only or isolated public-update soak with owned supervisors."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
from threading import Lock,Thread
import time
from uuid import uuid4

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.config import load_instruments
from inv_trend.data.provider_factory import create_default_providers
from inv_trend.monitoring.看板 import DashboardReader
from inv_trend.monitoring.扫描器 import CodeVersion
from inv_trend.monitoring.进程监督 import ProcessSupervisor
from inv_trend.monitoring.运行配置 import load_runtime_config
from inv_trend.notifications.渠道 import WebhookNotifier
from inv_trend.notifications.队列 import NotificationPolicy,NotificationQueue
from inv_trend.storage.结构化存储_v3 import UnifiedStore


def market_hashes(root):
    paths = {p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in {'.csv','.parquet'}}
    paths.update(p for p in (root/'current').rglob('*.json') if p.is_file())
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def write_json(path,value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers may briefly omit FILE_SHARE_DELETE. Keep the old complete
    # report until replacement succeeds; permanent access failures still surface.
    deadline = time.monotonic()+1.0
    while True:
        try:
            os.replace(temporary,path)
            return
        except OSError as exc:
            if os.name!='nt' or getattr(exc,'winerror',None) not in {5,32,33}:
                raise
            remaining = deadline-time.monotonic()
            if remaining<=0:
                raise
            time.sleep(min(.05,remaining))


def pending_deliveries(store,channel_id,policy):
    with store._connection() as db:
        rows = db.execute("SELECT o.*,e.payload AS event_payload,d.payload AS decision_payload "
            'FROM outbox o JOIN events e ON e.event_id=o.event_id '
            'JOIN decisions d ON d.decision_id=e.decision_id '
            "WHERE o.status IN ('PENDING','RETRY','IN_FLIGHT') "
            'AND (o.channel_id IS NULL OR o.channel_id=?)',(channel_id,)).fetchall()
    return sum(policy.allows(NotificationQueue._envelope(row)) for row in rows)


def soak_config(args,output):
    original = load_runtime_config(args.config)
    symbols = tuple(args.symbols) if args.symbols else original.symbols
    if args.refresh_isolated:
        # These are the public sources actually verified so far. Other assets
        # remain covered by read-only monitoring until their inputs are certified.
        if not symbols or set(symbols)-{'BTC','ETH'}:
            raise ValueError('isolated public-update acceptance requires explicit BTC/ETH symbols')
        data_root = output.resolve()/'隔离数据'
        if data_root.exists() or output.resolve().is_relative_to(original.data_root.resolve()):
            raise ValueError('isolated update root must be new and distinct from configured data')
    else:
        data_root = original.data_root
    return replace(original,data_root=data_root,runtime_dir=output.resolve()/'运行',
        symbols=symbols,refresh=args.refresh_isolated,paper_equity=None,
        interval_seconds=args.interval_seconds,poll_seconds=min(1.,args.interval_seconds)),original.data_root


def seed_isolated(config):
    # Start the same authoritative SQLite layout as the main Repository;
    # a bare root otherwise selects legacy DuckDB with cross-process locks.
    UnifiedStore(config.data_root,create=True)
    instruments = {symbol:load_instruments()[symbol] for symbol in config.symbols}
    repository = HistoricalDataService(config.data_root,instruments=instruments,
        providers=create_default_providers(timeout=10,retries=1,credential_providers=False))
    now = datetime.now(timezone.utc)
    for target in config.targets(repository):
        start = now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=400 if target.timeframe=='D1' else 90)
        result = repository.ingest(target.symbol,target.timeframe,start=start,end=now,retries=1)
        if result.quality_passed is not True or result.quality_status!='CURATED':
            raise RuntimeError('public seed failed formal certification: '+target.key)


def run(args):
    for value in (args.duration_seconds,args.interval_seconds,args.sample_seconds):
        if not math.isfinite(value) or value<=0: raise ValueError('soak intervals must be finite and positive')
    if type(args.minimum_cycles) is not int or args.minimum_cycles<1:
        raise ValueError('minimum completed cycles must be a positive integer')
    if type(args.minimum_bar_advances) is not int or args.minimum_bar_advances<0:
        raise ValueError('minimum bar advances must be a nonnegative integer')
    if args.minimum_bar_advances and not args.refresh_isolated:
        raise ValueError('bar advancement acceptance requires isolated updates')
    output = args.output.resolve()
    config,protected_root = soak_config(args,output)
    output.mkdir(parents=True,exist_ok=False)
    code = CodeVersion.capture()
    protected_before = market_hashes(protected_root)
    write_json(output/'受保护行情初始哈希.json',protected_before)
    if config.refresh:
        try:
            seed_isolated(config)
        except BaseException as exc:
            from inv_trend.observability.脱敏 import redact
            write_json(output/'持续验收状态.json',{
                'status':'FAILED','phase':'INITIALIZATION','source_hash':code.source_hash,
                'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'refresh':True,'data_root':str(config.data_root),'protected_data_root':str(protected_root),
                'error':type(exc).__name__+': '+redact(str(exc)),
                'protected_market_files_unchanged':market_hashes(protected_root)==protected_before,
                'owned_processes':[],'completion_scope':'initialization failed; observation never started'})
            raise
    before = market_hashes(config.data_root)
    write_json(output/'行情初始哈希.json',before)
    receiver_lock = Lock()
    receiver_ids = set()
    receiver_requests = [0]
    audit = (output/'本机接收审计.jsonl').open('a',encoding='utf-8')
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self,*unused): return None
        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0<size<=1048576: raise ValueError('invalid payload length')
                payload = self.rfile.read(size)
                body = json.loads(payload)
                identity = body['event_id']
                if identity!=self.headers.get('Idempotency-Key') or len(identity)!=64:
                    raise ValueError('invalid event identity')
                with receiver_lock:
                    duplicate = identity in receiver_ids
                    receiver_ids.add(identity)
                    receiver_requests[0] += 1
                    audit.write(json.dumps({'event_id':identity,'duplicate':duplicate,
                        'payload_sha256':hashlib.sha256(payload).hexdigest()},ensure_ascii=False)+'\n')
                    audit.flush()
                self.send_response(200)
                self.end_headers()
            except (ValueError,KeyError,TypeError):
                self.send_response(400)
                self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    thread = Thread(target=server.serve_forever,daemon=True)
    thread.start()
    environment_name = 'MONITOR_SOAK_LOCAL_' + uuid4().hex.upper()
    url = 'http://127.0.0.1:' + str(server.server_port) + '/验收_' + uuid4().hex
    # ASCII URL path; no user endpoint, provider credential or external receiver.
    url = url.replace('验收_','soak_')
    os.environ[environment_name] = url
    channel = WebhookNotifier(url)
    started = time.monotonic()
    report = {'status':'RUNNING','source_hash':code.source_hash,'git_commit':code.git_commit,
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'duration_requested_seconds':args.duration_seconds,'refresh':config.refresh,'paper_equity':None,
        'protected_data_root':str(protected_root.resolve()),'data_root':str(config.data_root.resolve()),
        'market_catalog_backend':'v3-sqlite' if config.refresh else 'configured Repository backend',
        'channel_id':channel.channel_id,'market_file_count':len(before),'samples':0,
        'scope':'Actual Repository, isolated EventStore, loopback notifications; no live orders',
        'completion_scope':'requested duration and minimum cycles only; not overall monitoring acceptance'}
    failure = None
    try:
        repository = HistoricalDataService(config.data_root,providers={})
        with ProcessSupervisor(config,webhook_env=environment_name,
                notification_channel_id=channel.channel_id) as notification:
            with ProcessSupervisor(config) as runtime:
                reader = DashboardReader(runtime.ledger.store,config,repository)
                next_sample = 0.
                while time.monotonic()-started<args.duration_seconds:
                    results = (runtime.tick(),notification.tick())
                    if any(r['status']=='EXHAUSTED' for r in results):
                        raise RuntimeError('owned supervisor restart budget exhausted')
                    elapsed = time.monotonic()-started
                    if elapsed>=next_sample:
                        if CodeVersion.capture().source_hash!=code.source_hash:
                            raise RuntimeError('source changed during soak; evidence no longer one code version')
                        model = reader.overview()
                        with runtime.ledger.store._connection() as db:
                            tasks = dict(db.execute('SELECT status,count(*) FROM runtime_tasks GROUP BY status').fetchall())
                            events = dict(db.execute('SELECT kind,count(*) FROM events GROUP BY kind').fetchall())
                        with receiver_lock:
                            received = {'requests':receiver_requests[0],'unique_events':len(receiver_ids)}
                        report.update(elapsed_seconds=elapsed,samples=report['samples']+1,
                            targets=len(model['targets']),tasks=tasks,events=events,
                            blocked_targets=sum(r['current_signal']=='BLOCKED' for r in model['targets']),
                            notification_queue=model['notification_queue'],receiver=received,
                            runtime_starts=len(runtime.processes()),notification_starts=len(notification.processes()))
                        write_json(output/'持续验收状态.json',report)
                        print(json.dumps({'elapsed_seconds':round(elapsed,2),'tasks':tasks,'receiver':received}),flush=True)
                        next_sample = elapsed+args.sample_seconds
                    time.sleep(.2)
            # Stop producers before draining eligible notifications. Suppressed
            # health/INFO events do not count as undelivered user notifications.
            policy = NotificationPolicy(**config.notifications)
            drain_deadline = time.monotonic()+30
            while time.monotonic()<drain_deadline:
                if notification.tick()['status']=='EXHAUSTED': raise RuntimeError('notification drain exhausted')
                if not pending_deliveries(runtime.ledger.store,channel.channel_id,policy): break
                time.sleep(.2)
            report['pending_deliverable_notifications'] = pending_deliveries(runtime.ledger.store,channel.channel_id,policy)
            if report['pending_deliverable_notifications']: raise RuntimeError('eligible notification drain timed out')
        # Sample final state after both owned children stop and recovery closes
        # interrupted tasks; the last periodic sample can still contain RUNNING.
        with runtime.ledger.store._connection() as db:
            report['tasks'] = dict(db.execute('SELECT status,count(*) FROM runtime_tasks GROUP BY status').fetchall())
            report['events'] = dict(db.execute('SELECT kind,count(*) FROM events GROUP BY kind').fetchall())
            report['notification_queue'] = dict(db.execute('SELECT status,count(*) FROM outbox GROUP BY status').fetchall())
            completed = {(r[0],r[1]):r[2] for r in db.execute(
                "SELECT symbol,timeframe,count(*) FROM runtime_tasks WHERE status IN ('SUCCESS','FAILED') "
                'AND finished_at IS NOT NULL GROUP BY symbol,timeframe')}
            unfinished = db.execute('SELECT count(*) FROM runtime_tasks WHERE finished_at IS NULL').fetchone()[0]
        targets = config.targets(repository)
        if config.refresh:
            with runtime.ledger.store._connection() as db:
                successful = {(r[0],r[1]):r[2] for r in db.execute(
                    "SELECT symbol,timeframe,count(*) FROM runtime_tasks WHERE status='SUCCESS' "
                    'AND finished_at IS NOT NULL GROUP BY symbol,timeframe')}
                snapshots = [json.loads(r[0]) for r in db.execute(
                    'SELECT DISTINCT d.payload FROM decisions d JOIN runtime_tasks t ON t.decision_id=d.decision_id '
                    "WHERE t.status='SUCCESS'")]
            advances = {}
            for target in targets:
                bars = sorted({s['bar_time'] for s in snapshots if s['symbol']==target.symbol and s['timeframe']==target.timeframe})
                advances[target.key] = max(0,len(bars)-1)
                if successful.get((target.symbol,target.timeframe),0)<args.minimum_cycles:
                    raise RuntimeError('successful update cycles below requested minimum: '+target.key)
                if advances[target.key]<args.minimum_bar_advances:
                    raise RuntimeError('observed closed-bar advances below requested minimum: '+target.key)
            for snap in snapshots:
                if snap['code_hash']!=code.source_hash or snap['conditions']['quality_gate'] is not True or snap['conditions']['freshness_gate'] is not True:
                    raise RuntimeError('successful update lacks verified fresh snapshot')
                if snap['conditions']['execution_gate'] is not False or snap['state_after']=='CONFIRMED_ENTRY':
                    raise RuntimeError('unknown account passed execution confirmation')
            catalog = repository.verify_catalog()
            if any(value for value in catalog.values() if isinstance(value,list)):
                raise RuntimeError('updated Catalog has consistency failures')
            report.update(successful_cycles={target.key:successful.get((target.symbol,target.timeframe),0) for target in targets},
                closed_bar_advances=advances,minimum_bar_advances=args.minimum_bar_advances,catalog=catalog)
        short = [r.key for r in targets if completed.get((r.symbol,r.timeframe),0)<args.minimum_cycles]
        report.update(minimum_cycles=args.minimum_cycles,targets_below_minimum_cycles=short,
            unfinished_tasks=unfinished,owned_processes=runtime.processes()+notification.processes(),
            minimum_observed_cycles=min((completed.get((r.symbol,r.timeframe),0) for r in targets),default=0))
        with receiver_lock:
            report['receiver'] = {'requests':receiver_requests[0],'unique_events':len(receiver_ids)}
        if short or unfinished: raise RuntimeError('requested minimum completed target cycles not achieved')
        from inv_trend.data.locking import _owner_process_state
        if any(r['finished_at'] is None or _owner_process_state({'pid':r['child_pid']}) is not False
                for r in report['owned_processes']):
            raise RuntimeError('owned process shutdown not verified')
        report['status'] = 'COMPLETED'
    except BaseException as exc:
        from inv_trend.observability.脱敏 import redact
        failure = exc
        report.update(status='FAILED',error=type(exc).__name__+': '+redact(str(exc)))
    finally:
        os.environ.pop(environment_name,None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        audit.close()
        after = market_hashes(config.data_root)
        changed = sorted(set(before)^set(after) | {name for name in before.keys() & after.keys() if before[name]!=after[name]})
        report.update(elapsed_seconds=time.monotonic()-started,market_files_unchanged=not changed,
            changed_market_files=changed)
        protected_after = market_hashes(protected_root)
        report['protected_market_files_unchanged'] = protected_after==protected_before
        if (changed and not config.refresh) or protected_after!=protected_before:
            report['status']='FAILED'
        write_json(output/'持续验收状态.json',report)
    if failure: raise failure
    if report['status']!='COMPLETED': raise RuntimeError('protected market content changed during soak')
    return report


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('config/监控运行示例.yaml'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration-seconds',type=float,default=86400)
    parser.add_argument('--interval-seconds',type=float,default=300)
    parser.add_argument('--sample-seconds',type=float,default=60)
    parser.add_argument('--minimum-cycles',type=int,default=2)
    parser.add_argument('--symbols',nargs='+',help='Optional read-only subset; explicit BTC/ETH for public-update acceptance')
    parser.add_argument('--refresh-isolated',action='store_true',help='Seed and update only a newly created data root inside output')
    parser.add_argument('--minimum-bar-advances',type=int,default=0)
    run(parser.parse_args())
