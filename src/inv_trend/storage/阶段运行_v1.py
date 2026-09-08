"""Additive stage/experiment schema on the EXISTING v3 SQLite authority, not a new DB."""
from __future__ import annotations

from dataclasses import asdict
import json
import zlib

from inv_trend.core.阶段协议_v1 import (
    Envelope, canonical, decode_envelope, digest, encode_envelope, utc_now,
)
from .结构化存储_v3 import UnifiedStore

DDL = """
CREATE TABLE IF NOT EXISTS stage_schema_v1(version INTEGER PRIMARY KEY, schema_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stage_studies_v1(
 study_id TEXT PRIMARY KEY, protocol_json TEXT NOT NULL, protocol_hash TEXT NOT NULL,
 dataset_version TEXT NOT NULL, code_version TEXT NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('FROZEN','COMPLETE','FAILED')),
 production_enabled INTEGER NOT NULL DEFAULT 0 CHECK(production_enabled=0));
CREATE TABLE IF NOT EXISTS stage_candidates_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, candidate_id TEXT NOT NULL,
 family TEXT NOT NULL, parameters_json TEXT NOT NULL, PRIMARY KEY(study_id,candidate_id));
CREATE TABLE IF NOT EXISTS stage_artifacts_v1(
 artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
 schema_version TEXT NOT NULL, stage_version TEXT NOT NULL, dataset_version TEXT NOT NULL,
 strategy_version TEXT NOT NULL, code_version TEXT NOT NULL, as_of TEXT NOT NULL,
 input_hash TEXT NOT NULL, output_hash TEXT NOT NULL, quality_status TEXT NOT NULL,
 generated_at TEXT NOT NULL, payload BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS stage_run_lookup_v1 ON stage_artifacts_v1(run_id,stage,as_of);
CREATE TABLE IF NOT EXISTS stage_lineage_v1(
 artifact_id TEXT NOT NULL REFERENCES stage_artifacts_v1,
 parent_id TEXT NOT NULL REFERENCES stage_artifacts_v1,
 PRIMARY KEY(artifact_id,parent_id));
CREATE TABLE IF NOT EXISTS stage_checkpoints_v1(
 run_id TEXT NOT NULL, stage TEXT NOT NULL, as_of TEXT NOT NULL,
 state_hash TEXT NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY(run_id,stage,as_of));
CREATE TABLE IF NOT EXISTS stage_trials_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, symbol TEXT NOT NULL, window_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL, fold INTEGER NOT NULL,
 train_start TEXT NOT NULL, train_end TEXT NOT NULL, metrics_json TEXT NOT NULL,
 score REAL NOT NULL, eligible INTEGER NOT NULL,
 PRIMARY KEY(study_id,symbol,window_id,candidate_id,fold),
 FOREIGN KEY(study_id,candidate_id) REFERENCES stage_candidates_v1(study_id,candidate_id));
CREATE TABLE IF NOT EXISTS stage_locks_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, symbol TEXT NOT NULL, family TEXT NOT NULL,
 window_id TEXT NOT NULL, cutoff TEXT NOT NULL, test_start TEXT NOT NULL, test_end TEXT NOT NULL,
 selection_json TEXT NOT NULL, lock_hash TEXT NOT NULL, committed_at TEXT NOT NULL,
 PRIMARY KEY(study_id,symbol,family,window_id),CHECK(cutoff<test_start));
CREATE TABLE IF NOT EXISTS stage_window_results_v1(
 study_id TEXT NOT NULL, symbol TEXT NOT NULL, family TEXT NOT NULL, window_id TEXT NOT NULL,
 scenario TEXT NOT NULL, metrics_json TEXT NOT NULL, start_equity REAL NOT NULL,
 end_equity REAL NOT NULL, PRIMARY KEY(study_id,symbol,family,window_id,scenario),
 FOREIGN KEY(study_id,symbol,family,window_id) REFERENCES stage_locks_v1(study_id,symbol,family,window_id));
CREATE TABLE IF NOT EXISTS stage_curves_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, symbol TEXT NOT NULL, family TEXT NOT NULL,
 scenario TEXT NOT NULL, as_of TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,
 quantity REAL NOT NULL, fees REAL NOT NULL, slippage_cost REAL NOT NULL,
 PRIMARY KEY(study_id,symbol,family,scenario,as_of));
CREATE TABLE IF NOT EXISTS stage_fills_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, symbol TEXT NOT NULL, family TEXT NOT NULL,
 scenario TEXT NOT NULL, order_id TEXT NOT NULL, filled_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(study_id,symbol,family,scenario,order_id,filled_at));
CREATE TABLE IF NOT EXISTS stage_decisions_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, symbol TEXT NOT NULL, family TEXT NOT NULL,
 window_id TEXT NOT NULL, as_of TEXT NOT NULL, candidate_id TEXT NOT NULL,
 output_hash TEXT NOT NULL, payload BLOB NOT NULL,
 PRIMARY KEY(study_id,symbol,family,as_of),
 FOREIGN KEY(study_id,symbol,family,window_id) REFERENCES stage_locks_v1(study_id,symbol,family,window_id));
CREATE TABLE IF NOT EXISTS stage_reviews_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, review_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(study_id,review_id));
CREATE TABLE IF NOT EXISTS stage_queries_v1(
 study_id TEXT NOT NULL REFERENCES stage_studies_v1, ordinal INTEGER NOT NULL,
 payload_json TEXT NOT NULL, PRIMARY KEY(study_id,ordinal));
"""


class StageRepository:
    def __init__(self, data_root, *, create=False):
        self.store = UnifiedStore(data_root, create=create)
        with self.store.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            old = db.execute("SELECT schema_hash FROM stage_schema_v1 WHERE version=1").fetchone()
            if old and old[0] != digest(DDL):
                raise ValueError("stage schema checksum changed; explicit migration required")
            db.execute("INSERT OR IGNORE INTO stage_schema_v1 VALUES(1,?)", (digest(DDL),))

    def freeze(self, study_id: str, protocol: dict, candidates: tuple) -> None:
        # Reuse/overwrite is prohibited: a re-run must have a distinct immutable study ID.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO stage_studies_v1 VALUES(?,?,?,?,?,?,?,0)",
                       (study_id, canonical(protocol).decode(), digest(protocol),
                        protocol["dataset_version"], protocol["code_version"], utc_now(), "FROZEN"))
            db.executemany("INSERT INTO stage_candidates_v1 VALUES(?,?,?,?)",
                           [(study_id, c.id, c.family, canonical(c).decode()) for c in candidates])
            self.store.audit("STAGE_PROTOCOL_FROZEN", study_id,
                             {"protocol_hash": digest(protocol), "candidates": len(candidates)}, db=db)

    def publish(self, packets: list[Envelope], checkpoints: list[tuple] = ()) -> None:
        # Whole bar or whole window publication is transactional. Parents must already exist
        # or precede children within this transaction; retries cannot rewrite old artifacts.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for packet in packets:
                raw = encode_envelope(packet)
                old = db.execute("SELECT payload FROM stage_artifacts_v1 WHERE artifact_id=?",
                                 (packet.artifact_id,)).fetchone()
                if old:
                    previous = decode_envelope(zlib.decompress(old[0]))
                    if previous.artifact_id != packet.artifact_id:
                        raise ValueError("artifact identity conflict")
                    continue
                c = packet.context
                db.execute("INSERT INTO stage_artifacts_v1 VALUES(" + ",".join("?" * 14) + ")",
                           (packet.artifact_id, c.run_id, packet.stage, packet.schema_version,
                            packet.stage_version, c.dataset_version, c.strategy_version, c.code_version,
                            packet.as_of, packet.input_hash, packet.output_hash, packet.quality.status,
                            packet.generated_at, zlib.compress(raw, 3)))
                db.executemany("INSERT INTO stage_lineage_v1 VALUES(?,?)",
                               [(packet.artifact_id, parent) for parent in packet.parent_ids])
            for run, stage, as_of, state in checkpoints:
                text = canonical(state).decode()
                old = db.execute("SELECT state_hash FROM stage_checkpoints_v1 WHERE run_id=? AND stage=? AND as_of=?",
                                 (run, stage, as_of)).fetchone()
                if old and old[0] != digest(state):
                    raise ValueError("checkpoint is immutable")
                db.execute("INSERT OR IGNORE INTO stage_checkpoints_v1 VALUES(?,?,?,?,?)",
                           (run, stage, as_of, digest(state), text))

    def load(self, artifact_id: str) -> Envelope:
        rows = self.store.rows("SELECT payload FROM stage_artifacts_v1 WHERE artifact_id=?", (artifact_id,))
        if not rows:
            raise KeyError(artifact_id)
        packet = decode_envelope(zlib.decompress(rows[0]["payload"]))
        if packet.artifact_id != artifact_id:
            raise ValueError("stored artifact metadata/content was modified")
        return packet

    def checkpoint(self, run: str, stage: str, as_of: str) -> dict:
        rows = self.store.rows("SELECT * FROM stage_checkpoints_v1 WHERE run_id=? AND stage=? AND as_of=?",
                               (run, stage, as_of))
        if not rows:
            raise KeyError((run, stage, as_of))
        state = json.loads(rows[0]["state_json"])
        if digest(state) != rows[0]["state_hash"]:
            raise ValueError("checkpoint integrity failure")
        return state

    def training(self, study: str, symbol: str, window: str, rows: list[dict]) -> None:
        with self.store.connect() as db:
            db.executemany("INSERT INTO stage_trials_v1 VALUES(" + ",".join("?" * 10) + ")",
                           [(study, symbol, window, r["candidate_id"], r["fold"], r["start"], r["end"],
                             canonical(r["metrics"]).decode(), r["score"], int(r["eligible"])) for r in rows])

    def lock(self, study: str, symbol: str, family: str, window: dict, selection: dict) -> str:
        lock = {"window": window, "selection": selection, "symbol": symbol, "family": family}
        h = digest(lock)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO stage_locks_v1 VALUES(" + ",".join("?" * 10) + ")",
                       (study, symbol, family, window["id"], window["cutoff"], window["start"],
                        window["end"], canonical(selection).decode(), h, utc_now()))
            self.store.audit("STAGE_PARAMETERS_LOCKED", study, {"lock_hash": h, **lock}, db=db)
        return h

    def results(self, study, symbol, family, window_id, scenario, snapshots, initial_equity, metrics):
        if not snapshots:
            raise ValueError("cannot publish an empty window as a successful backtest")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO stage_window_results_v1 VALUES(?,?,?,?,?,?,?,?)",
                       (study, symbol, family, window_id, scenario, canonical(metrics).decode(),
                        initial_equity, snapshots[-1].equity))
            # Intentionally use explicit column count to fail on incompatible migrations.
            db.executemany("INSERT INTO stage_curves_v1 VALUES(?,?,?,?,?,?,?,?,?,?)",
                           [(study, symbol, family, scenario, s.as_of, s.equity, s.cash, s.quantity,
                             s.fees, s.slippage_cost) for s in snapshots])
            db.executemany("INSERT INTO stage_fills_v1 VALUES(?,?,?,?,?,?,?)",
                           [(study, symbol, family, scenario, f.order_id, f.filled_at,
                             canonical(f).decode()) for s in snapshots for f in s.fills])

    def decisions(self, study, symbol, family, window, signals):
        with self.store.connect() as db:
            db.executemany("INSERT INTO stage_decisions_v1 VALUES(?,?,?,?,?,?,?,?)",
                           [(study, symbol, family, window, s.features.bar.available_at,
                             s.candidate_id, digest(s), zlib.compress(canonical(s), 3)) for s in signals])

    def review(self, study, review_id, payload):
        with self.store.connect() as db:
            db.execute("INSERT INTO stage_reviews_v1 VALUES(?,?,?,?,?)",
                       (study, review_id, canonical(payload).decode(), digest(payload), utc_now()))
            self.store.audit("STAGE_REVIEW", study, {"review_id": review_id,
                                                     "hash": digest(payload)}, db=db)

    def finish(self, study, queries, *, failed=False):
        with self.store.connect() as db:
            db.executemany("INSERT INTO stage_queries_v1 VALUES(?,?,?)",
                           [(study, i, canonical(q).decode()) for i, q in enumerate(queries)])
            db.execute("UPDATE stage_studies_v1 SET status=? WHERE study_id=? AND status='FROZEN'",
                       ("FAILED" if failed else "COMPLETE", study))
            self.store.audit("STAGE_STUDY_FINISHED", study, {"failed": failed, "queries": len(queries)}, db=db)
