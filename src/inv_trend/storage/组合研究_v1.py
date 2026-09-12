"""Append-only portfolio evidence in the existing SQLite authority."""
from dataclasses import asdict
import json

from .结构化存储_v3 import UnifiedStore, canonical, digest, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS universes(universe_id TEXT, version TEXT, payload TEXT NOT NULL,
 PRIMARY KEY(universe_id,version));
CREATE TABLE IF NOT EXISTS instrument_contracts(instrument_id TEXT,version TEXT,
 quality_status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(instrument_id,version));
CREATE TABLE IF NOT EXISTS portfolio_studies(study_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,
 mode TEXT NOT NULL,status TEXT NOT NULL,protocol TEXT NOT NULL,protocol_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_runs(run_id TEXT PRIMARY KEY,study_id TEXT NOT NULL REFERENCES portfolio_studies,
 label TEXT NOT NULL,scope TEXT NOT NULL,parameters TEXT NOT NULL,metrics TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_equity(run_id TEXT REFERENCES portfolio_runs,timestamp TEXT,
 cash REAL NOT NULL,equity REAL NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,timestamp));
CREATE TABLE IF NOT EXISTS portfolio_positions(run_id TEXT REFERENCES portfolio_runs,timestamp TEXT,
 instrument_id TEXT,quantity REAL NOT NULL,PRIMARY KEY(run_id,timestamp,instrument_id));
CREATE TABLE IF NOT EXISTS portfolio_orders(run_id TEXT REFERENCES portfolio_runs,order_id TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(run_id,order_id));
CREATE TABLE IF NOT EXISTS portfolio_fills(run_id TEXT REFERENCES portfolio_runs,ordinal INTEGER,
 payload TEXT NOT NULL,PRIMARY KEY(run_id,ordinal));
CREATE TABLE IF NOT EXISTS portfolio_risk(run_id TEXT REFERENCES portfolio_runs,timestamp TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(run_id,timestamp));
CREATE TABLE IF NOT EXISTS portfolio_checkpoints(study_id TEXT REFERENCES portfolio_studies,
 window_id TEXT,family TEXT,scenario TEXT,code_version TEXT NOT NULL,dataset_version TEXT NOT NULL,
 payload TEXT NOT NULL,PRIMARY KEY(study_id,window_id,family,scenario));
CREATE TABLE IF NOT EXISTS parameter_trials(study_id TEXT REFERENCES portfolio_studies,window_id TEXT,
 experiment_id TEXT,family TEXT NOT NULL,parameters TEXT NOT NULL,metrics TEXT NOT NULL,
 status TEXT NOT NULL,error TEXT,PRIMARY KEY(study_id,window_id,experiment_id));
CREATE TABLE IF NOT EXISTS parameter_locks(parameter_lock_id TEXT PRIMARY KEY,
 study_id TEXT NOT NULL REFERENCES portfolio_studies,window_id TEXT NOT NULL,family TEXT NOT NULL,
 train_start TEXT NOT NULL,train_end TEXT NOT NULL,oos_start TEXT NOT NULL,oos_end TEXT NOT NULL,
 selected_params TEXT NOT NULL,selection_score REAL,dataset_version TEXT NOT NULL,
 strategy_version TEXT NOT NULL,code_version TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(study_id,window_id,family),CHECK(train_start<train_end AND train_end<=oos_start AND oos_start<oos_end));
CREATE TABLE IF NOT EXISTS walk_forward_windows(study_id TEXT REFERENCES portfolio_studies,window_id TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(study_id,window_id));
CREATE TABLE IF NOT EXISTS benchmarks(study_id TEXT REFERENCES portfolio_studies,label TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(study_id,label));
CREATE TABLE IF NOT EXISTS stress_tests(study_id TEXT REFERENCES portfolio_studies,family TEXT,scenario TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(study_id,family,scenario));
CREATE TABLE IF NOT EXISTS reviews(study_id TEXT REFERENCES portfolio_studies,version TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(study_id,version));
CREATE TABLE IF NOT EXISTS review_scores(study_id TEXT REFERENCES portfolio_studies,version TEXT,dimension TEXT,
 score REAL NOT NULL CHECK(score>=0 AND score<=10),PRIMARY KEY(study_id,version,dimension));
CREATE TABLE IF NOT EXISTS lineage(study_id TEXT REFERENCES portfolio_studies,instrument_id TEXT,
 payload TEXT NOT NULL,PRIMARY KEY(study_id,instrument_id));
CREATE TABLE IF NOT EXISTS reports(study_id TEXT REFERENCES portfolio_studies,name TEXT,
 content BLOB NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(study_id,name));
"""


def encoded(value):
    return canonical(value).decode("utf-8")


class PortfolioRepository:
    def __init__(self, data_root="data"):
        self.store = UnifiedStore(data_root)
        with self.store.connect() as db:
            db.executescript(SCHEMA)
            for table in ("parameter_locks", "parameter_trials", "universes", "instrument_contracts",
                          "portfolio_runs", "portfolio_equity", "portfolio_positions", "portfolio_orders",
                          "portfolio_fills", "portfolio_risk", "walk_forward_windows", "benchmarks",
                          "stress_tests", "reviews", "review_scores", "lineage", "reports", "portfolio_checkpoints"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT,'immutable portfolio evidence'); END")

    def begin(self, study_id, protocol, contracts, lineage):
        with self.store.connect() as db:
            db.execute("INSERT INTO portfolio_studies VALUES(?,?,?,?,?,?)",
                       (study_id, utcnow(), protocol["mode"], "RUNNING", encoded(protocol), digest(canonical(protocol))))
            universe = {"core": protocol["core"], "purpose": "fixed_core_never_replaced"}
            payload = encoded(universe)
            existing = db.execute("SELECT payload FROM universes WHERE universe_id=? AND version=?",
                                  ("BTC_ETH_XAU_XAG", "1.0")).fetchone()
            if existing and existing[0] != payload:
                raise ValueError("universe version collision")
            db.execute("INSERT OR IGNORE INTO universes VALUES(?,?,?)", ("BTC_ETH_XAU_XAG", "1.0", payload))
            for c in contracts:
                payload = encoded(asdict(c))
                old = db.execute("SELECT payload FROM instrument_contracts WHERE instrument_id=? AND version=?",
                                 (c.instrument_id, c.version)).fetchone()
                if old and old[0] != payload:
                    raise ValueError("contract version collision")
                db.execute("INSERT OR IGNORE INTO instrument_contracts VALUES(?,?,?,?)",
                           (c.instrument_id, c.version, c.quality_status, payload))
            db.executemany("INSERT INTO lineage VALUES(?,?,?)",
                           [(study_id, s, encoded(v)) for s, v in lineage.items()])

    def trials(self, study_id, window, rows):
        with self.store.connect() as db:
            db.executemany("INSERT INTO parameter_trials VALUES(?,?,?,?,?,?,?,?)", [
                (study_id, window, c.id, c.family, encoded(asdict(c)), encoded(metrics), status, error)
                for c, metrics, status, error in rows])

    def lock(self, study_id, window, family, candidate, score, dataset, code):
        from inv_trend.core.四资产合同_v1 import utc
        if not utc(window["train_start"]) < utc(window["train_end"]) <= utc(window["oos_start"]) < utc(window["oos_end"]):
            raise ValueError("train/oos separation invalid")
        lock_id = digest(canonical((study_id, window, family, asdict(candidate), dataset, code)))
        with self.store.connect() as db:
            db.execute("INSERT INTO parameter_locks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (lock_id, study_id, window["id"], family, window["train_start"], window["train_end"],
                        window["oos_start"], window["oos_end"], encoded(asdict(candidate)), score,
                        dataset, "four-asset-v1", code, utcnow()))
        return lock_id

    def run(self, study_id, label, scope, parameters, metrics, account, curve, risks):
        run_id = study_id + "/" + label
        with self.store.connect() as db:
            db.execute("INSERT INTO portfolio_runs VALUES(?,?,?,?,?,?)",
                       (run_id, study_id, label, scope, encoded(parameters), encoded(metrics)))
            db.executemany("INSERT INTO portfolio_equity VALUES(?,?,?,?,?)",
                           [(run_id, r["timestamp"], r["cash"], r["equity"], encoded(r)) for r in curve])
            db.executemany("INSERT INTO portfolio_positions VALUES(?,?,?,?)", [
                (run_id, r["timestamp"], account.contracts[s].instrument_id, q)
                for r in curve for s, q in r["positions"].items()])
            db.executemany("INSERT INTO portfolio_orders VALUES(?,?,?)",
                           [(run_id, r["order_id"], encoded(r)) for r in account.orders])
            db.executemany("INSERT INTO portfolio_fills VALUES(?,?,?)",
                           [(run_id, i, encoded(r)) for i, r in enumerate(account.fills)])
            db.executemany("INSERT INTO portfolio_risk VALUES(?,?,?)",
                           [(run_id, r["timestamp"], encoded(r)) for r in risks])
        return run_id

    def record(self, table, keys, payload):
        if table not in ("walk_forward_windows", "benchmarks", "stress_tests", "reviews"):
            raise ValueError("unsupported evidence table")
        with self.store.connect() as db:
            db.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in range(len(keys)+1))})",
                       (*keys, encoded(payload)))

    def finish(self, study_id, status):
        if status not in ("RESEARCH_COMPLETE_FORMAL_BLOCKED", "FAILED"):
            raise ValueError("invalid completion status")
        with self.store.connect() as db:
            db.execute("UPDATE portfolio_studies SET status=? WHERE study_id=? AND status='RUNNING'",
                       (status, study_id))

    def checkpoint(self, study_id, window, family, scenario, code, dataset, account):
        with self.store.connect() as db:
            db.execute("INSERT INTO portfolio_checkpoints VALUES(?,?,?,?,?,?,?)",
                       (study_id, window, family, scenario, code, dataset, encoded(account.checkpoint())))

    def report(self, study_id, name, content):
        raw = content.encode("utf-8")
        with self.store.connect() as db:
            db.execute("INSERT INTO reports VALUES(?,?,?,?)", (study_id, name, raw, digest(raw)))

    def read_report(self, study_id):
        rows = self.store.rows("SELECT payload FROM reviews WHERE study_id=? AND version='v1'", (study_id,))
        return json.loads(rows[0]["payload"])
