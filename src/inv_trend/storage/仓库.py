"""Content-addressed objects and recoverable file-before-database publication."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import os
import sqlite3
from uuid import uuid4

from inv_trend.data.locking import FileLock
from .基础 import (Lease, atomic_write, beneath, closing_connection, digest, encoded,
                 file_hash, lease_active, now, redact, segment, sync_directory, transaction)


class StorageIntegrityError(RuntimeError):
    pass


class Storage:
    def __init__(self, root="."):
        self.root = Path(root).resolve()
        self.system = self.root / "outputs" / "系统"
        self.system.mkdir(parents=True, exist_ok=True)
        self.path = self.system / "运行结果.sqlite3"
        self.objects = self.system / "对象" / "sha256"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.system / "提交与回收.lock"
        with self.lock():
            with closing_connection(self.path) as db:
                db.execute("PRAGMA journal_mode=WAL")
                schema = Path(__file__).with_name("结构.sql").read_text(encoding="utf-8")
                checksum = hashlib.sha256(schema.encode()).hexdigest()
                db.executescript(schema)
                old = db.execute("SELECT checksum FROM schema_migrations WHERE version=1").fetchone()
                if old and old[0] != checksum:
                    raise StorageIntegrityError("schema changed without a migration")
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?,?)", (now(), checksum))

    def lock(self):
        return FileLock(self.lock_path, timeout=30)

    def rows(self, sql, args=()):
        with closing_connection(self.path) as db:
            return [dict(row) for row in db.execute(sql, args)]

    def tx(self, callback):
        return transaction(self.path, callback)

    def blob_path(self, value):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid SHA-256")
        return self.objects / value[:2] / value[2:4] / value

    def _relative(self, path):
        return beneath(self.root, path).relative_to(self.root).as_posix()

    def _path(self, relative):
        return beneath(self.root, self.root / relative)

    def _put(self, source):
        value = file_hash(source)
        target = self.blob_path(value)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_hash(target) != value:
                raise StorageIntegrityError(f"corrupt existing object: {value}")
        else:
            os.replace(source, target)
            sync_directory(target.parent)
        return value, target

    def publish(self, task, run_id, report_date, files, *, facts=None, config=None,
                retention_class="RESULT", protection_reason="正式运行", fault=None):
        """files maps logical names to bytes. Schema validation precedes registration.

        fault(phase) is an injection seam; no business calculation is repeated by recovery.
        """
        task, run_id, report_date = map(segment, (task, run_id, report_date))
        facts = redact(facts or {})
        identity = digest({"task": task, "date": report_date, "files": {
            k: hashlib.sha256(v).hexdigest() for k, v in files.items()}, "facts": facts,
            "config": redact(config or {})})
        stage = self.root / ".tmp" / run_id
        stage.mkdir(parents=True, exist_ok=True)
        # A run lock prevents one lease replacing another live owner's heartbeat.
        with FileLock(self.system / f"运行-{run_id}.lock", timeout=30):
            with Lease(stage, run_id) as lease:
                with self.lock():
                    old = self.rows("SELECT * FROM runs WHERE run_id=?", (run_id,))
                    if old and old[0]["manifest_hash"]:
                        manifest = self.manifest(run_id)
                        if manifest["identity"] != identity:
                            raise StorageIntegrityError("immutable run identity conflict")
                        return self._recover_one(manifest, fault)
                    self.tx(lambda db: (
                        db.execute("INSERT OR IGNORE INTO runs(run_id,task,report_date,created_at,config_hash) VALUES(?,?,?,?,?)",
                                   (run_id, task, report_date, now(), digest(redact(config or {})))),
                        db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (run_id, self._relative(lease.path), lease.row["token"]))
                    ))
                if fault:
                    fault("STAGING")
                prepared = []
                for name, content in sorted(files.items()):
                    # Logical names may have folders but never escape the run.
                    beneath(stage, stage / name)
                    info = self._validate(content, Path(name).suffix)
                    temp = stage / f"{uuid4().hex}.stage"
                    atomic_write(temp, content)
                    prepared.append((name, temp, info))
                with self.lock():
                    entries = []
                    for name, temp, info in prepared:
                        value, target = self._put(temp)
                        entries.append(dict(logical_name=name, hash=value, size=target.stat().st_size,
                                            path=self._relative(target), **info))
                    manifest = dict(schema_version=3, task=task, run_id=run_id, report_date=report_date,
                                    identity=identity, files=entries, facts=facts, config=redact(config or {}),
                                    retention_class=retention_class, protection_reason=protection_reason)
                    manifest_path = self.root / "outputs" / task / "runs" / report_date / run_id / "审计" / "运行清单_v3.json"
                    if manifest_path.exists() and manifest_path.read_bytes() != encoded(manifest):
                        raise StorageIntegrityError("immutable manifest conflict")
                    atomic_write(manifest_path, encoded(manifest))
                    self.tx(lambda db: (
                        db.execute("UPDATE runs SET phase='FILES_PUBLISHED',manifest_hash=?,manifest_path=? WHERE run_id=?",
                                   (digest(manifest), self._relative(manifest_path), run_id)),
                        db.execute("INSERT INTO publication_events(run_id,phase,recorded_at) VALUES(?,'FILES_PUBLISHED',?)", (run_id, now()))
                    ))
                    if fault:
                        fault("FILES_PUBLISHED")
                    return self._recover_one(manifest, fault)

    @staticmethod
    def _validate(content, suffix):
        info = dict(format=suffix.lstrip(".") or "binary", schema_version=None, schema_hash=None)
        if suffix == ".json":
            value = json.loads(content)
            if isinstance(value, dict):
                info["schema_version"] = str(value.get("schema_version", "legacy"))
        elif suffix == ".parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq
            schema = pq.read_schema(pa.BufferReader(content))
            metadata = schema.metadata or {}
            info["schema_version"] = metadata.get(b"schema_version", b"legacy").decode()
            info["schema_hash"] = hashlib.sha256(schema.remove_metadata().serialize().to_pybytes()).hexdigest()
            if b"schema_hash" in metadata and metadata[b"schema_hash"].decode() != info["schema_hash"]:
                raise StorageIntegrityError("Parquet schema hash mismatch")
        return info

    def manifest(self, run_id):
        rows = self.rows("SELECT manifest_path,manifest_hash FROM runs WHERE run_id=?", (run_id,))
        if not rows or not rows[0]["manifest_path"]:
            raise KeyError(run_id)
        data = self._path(rows[0]["manifest_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != rows[0]["manifest_hash"]:
            raise StorageIntegrityError("manifest hash mismatch")
        return json.loads(data)

    def _verify_manifest(self, manifest):
        for item in manifest["files"]:
            path = self._path(item["path"])
            if path != self.blob_path(item["hash"]) or not path.is_file() or file_hash(path) != item["hash"]:
                raise StorageIntegrityError(f"missing/corrupt blob: {item['hash']}")

    def _recover_one(self, manifest, fault=None):
        run_id = manifest["run_id"]
        try:
            self._verify_manifest(manifest)
        except StorageIntegrityError as exc:
            self.tx(lambda db: db.execute("UPDATE runs SET integrity_error=?,status='INTEGRITY_FAILED' WHERE run_id=?", (str(exc), run_id)))
            raise
        state = self.rows("SELECT * FROM runs WHERE run_id=?", (run_id,))[0]
        if state["retired_at"]:
            raise StorageIntegrityError("retired run cannot be republished")
        if state["phase"] in {"STAGING", "FILES_PUBLISHED"}:
            def commit(db):
                for item in manifest["files"]:
                    db.execute("INSERT OR IGNORE INTO blobs(hash,path,size,format,schema_version,schema_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                               (item["hash"], item["path"], item["size"], item["format"], item["schema_version"], item["schema_hash"], now()))
                    # A previously quarantined identity must have been restored before use.
                    db.execute("UPDATE blobs SET state='LIVE',quarantine_path=NULL,quarantined_at=NULL WHERE hash=?", (item["hash"],))
                    artifact_id = digest([run_id, item["logical_name"]])
                    db.execute("INSERT INTO artifacts VALUES(?,?,?,3)", (artifact_id, item["hash"], item["logical_name"]))
                    root_id = "run:" + run_id
                    db.execute("INSERT OR IGNORE INTO retention_roots(root_id,run_id,retention_class,protection_reason) VALUES(?,?,?,?)",
                               (root_id, run_id, manifest["retention_class"], manifest["protection_reason"]))
                    db.execute("INSERT INTO artifact_refs VALUES(?,?,?)", (root_id, artifact_id, item["logical_name"]))
                self._facts(db, run_id, manifest.get("facts", {}))
                seq = db.execute("SELECT COALESCE(MAX(commit_seq),0)+1 FROM runs").fetchone()[0]
                db.execute("UPDATE runs SET phase='DB_COMMITTED',committed_at=?,commit_seq=?,integrity_error=NULL WHERE run_id=?", (now(), seq, run_id))
                db.execute("INSERT INTO publication_events(run_id,phase,recorded_at) VALUES(?,'DB_COMMITTED',?)", (run_id, now()))
            self.tx(commit)
        if fault:
            fault("DB_COMMITTED")
        self.tx(lambda db: (
            db.execute("UPDATE runs SET status='SUCCEEDED',phase='RUN_SUCCEEDED' WHERE run_id=? AND phase!='LATEST_UPDATED'", (run_id,)),
            db.execute("INSERT INTO publication_events(run_id,phase,recorded_at) VALUES(?,'RUN_SUCCEEDED',?)", (run_id, now()))
        ))
        if fault:
            fault("RUN_SUCCEEDED")
        self._latest(manifest["task"])
        self.tx(lambda db: db.execute("UPDATE runs SET phase='LATEST_UPDATED' WHERE run_id=?", (run_id,)))
        if fault:
            fault("LATEST_UPDATED")
        return {item["logical_name"]: self._path(item["path"]) for item in manifest["files"]}

    def _facts(self, db, run_id, facts):
        for dataset in facts.get("datasets", []):
            key = digest(dataset)
            db.execute("INSERT OR IGNORE INTO dataset_refs VALUES(?,?,?,?,?)", (key, dataset["version"], dataset["checksum"], dataset["manifest_path"], encoded(dataset).decode()))
            db.execute("INSERT OR IGNORE INTO run_inputs VALUES(?,?,?)", (run_id, key, dataset.get("role", "MARKET")))
        for experiment in facts.get("experiments", []):
            params = redact(experiment.get("parameters", {}))
            ph = digest(params)
            db.execute("INSERT OR IGNORE INTO parameter_sets VALUES(?,?)", (ph, encoded(params).decode()))
            for name, value in params.items():
                db.execute("INSERT OR IGNORE INTO parameter_values VALUES(?,?,?,?,?)", (ph, name, type(value).__name__, value if isinstance(value, (float, int)) else None, encoded(value).decode()))
            eid = digest([run_id, experiment["id"]])
            db.execute("INSERT INTO experiments(experiment_id,run_id,combination_id,parameter_hash,sample_status,kind,assumptions_hash,result_hash,status,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (eid, run_id, experiment["id"], ph, experiment.get("sample_status", "UNKNOWN"), experiment.get("kind", "RESEARCH"), experiment.get("assumptions_hash"), experiment.get("result_hash"), experiment.get("status", "COMPLETED"), encoded(experiment).decode()))
            for metric in experiment.get("metrics", []):
                scenario = metric.get("scenario", {})
                sid = digest(scenario)
                db.execute("INSERT OR IGNORE INTO scenarios VALUES(?,?)", (sid, encoded(scenario).decode()))
                db.execute("INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?)", (eid, metric.get("instrument", "ALL"), metric["sample_role"], str(metric.get("window_id", "NA")), str(metric.get("fold_id", "NA")), sid, metric["metric_name"], metric["value"]))

    def _latest(self, task):
        rows = self.rows("SELECT run_id,committed_at,manifest_hash FROM runs WHERE task=? AND status='SUCCEEDED' AND retired_at IS NULL AND manifest_hash IS NOT NULL ORDER BY commit_seq DESC LIMIT 1", (task,))
        if rows:
            atomic_write(self.root / "outputs" / task / "latest" / "latest.json", encoded(rows[0]))

    def recover(self):
        results = []
        with self.lock():
            for row in self.rows("SELECT * FROM runs WHERE manifest_path IS NOT NULL AND retired_at IS NULL"):
                try:
                    self._recover_one(self.manifest(row["run_id"]))
                    results.append(dict(run_id=row["run_id"], status="RECOVERED"))
                except (StorageIntegrityError, OSError) as exc:
                    results.append(dict(run_id=row["run_id"], status="FAILED", error=str(exc)))
        return results

    def verify(self):
        errors = []
        with self.lock():
            for row in self.rows("PRAGMA integrity_check"):
                if list(row.values()) != ["ok"]:
                    errors.append(row)
            errors.extend(self.rows("PRAGMA foreign_key_check"))
            for row in self.rows("SELECT * FROM runs WHERE manifest_path IS NOT NULL AND retired_at IS NULL"):
                try:
                    self._verify_manifest(self.manifest(row["run_id"]))
                except (OSError, StorageIntegrityError) as exc:
                    errors.append(dict(run_id=row["run_id"], error=str(exc)))
        return dict(ok=not errors, errors=errors)

    def protect(self, run_id, root_id, retention_class, reason, *, signal_key=None):
        with self.lock():
            def write(db):
                db.execute("INSERT INTO retention_roots(root_id,run_id,retention_class,protection_reason) VALUES(?,?,?,?)", (root_id, run_id, retention_class, reason))
                db.execute("INSERT INTO artifact_refs SELECT ?,artifact_id,logical_name FROM artifact_refs WHERE root_id=?", (root_id, "run:" + run_id))
                if signal_key:
                    db.execute("INSERT INTO signal_protections(signal_key,root_id) VALUES(?,?)", (signal_key, root_id))
            self.tx(write)

    def retire(self, run_id):
        with self.lock():
            row = self.rows("SELECT * FROM runs WHERE run_id=?", (run_id,))[0]
            latest = self.root / "outputs" / row["task"] / "latest" / "latest.json"
            if latest.exists() and json.loads(latest.read_text(encoding="utf-8"))["run_id"] == run_id:
                raise ValueError("current successful run cannot be retired")
            self.tx(lambda db: (
                db.execute("UPDATE runs SET retired_at=? WHERE run_id=?", (now(), run_id)),
                db.execute("UPDATE retention_roots SET retired_at=? WHERE root_id=?", (now(), "run:" + run_id))
            ))

    def gc(self, *, apply=False):
        """Mark/sweep, conservative lease protection, recoverable quarantine."""
        result = []
        with self.lock():
            if self.rows("SELECT run_id FROM runs WHERE integrity_error IS NOT NULL"):
                raise StorageIntegrityError("GC blocked by integrity failures")
            if any(lease_active(self._path(r["path"])) for r in self.rows("SELECT * FROM leases")):
                return [dict(status="BLOCKED_ACTIVE_OR_UNKNOWN_LEASE")]
            reachable = {r["hash"] for r in self.rows("SELECT DISTINCT a.blob_hash AS hash FROM artifacts a JOIN artifact_refs f USING(artifact_id) JOIN retention_roots r USING(root_id) WHERE r.retired_at IS NULL")}
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            for blob in self.rows("SELECT * FROM blobs WHERE state!='DELETED'"):
                if blob["hash"] in reachable or blob["created_at"] > cutoff:
                    continue
                source = self._path(blob["path"])
                quarantine = self.system / "隔离" / blob["hash"]
                action = "QUARANTINE" if blob["state"] == "LIVE" else "DELETE"
                if action == "DELETE" and (blob["quarantined_at"] or now()) > cutoff:
                    continue
                result.append(dict(hash=blob["hash"], action=action, applied=apply))
                if not apply:
                    continue
                if action == "QUARANTINE":
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    # Persist intent first; recovery recognises either file location.
                    self.tx(lambda db: db.execute("UPDATE blobs SET state='QUARANTINING',quarantine_path=?,quarantined_at=? WHERE hash=?", (self._relative(quarantine), now(), blob["hash"])))
                    if source.exists():
                        os.replace(source, quarantine)
                    self.tx(lambda db: db.execute("UPDATE blobs SET state='QUARANTINED' WHERE hash=?", (blob["hash"],)))
                else:
                    if blob["state"] == "QUARANTINING":
                        if source.exists():
                            os.replace(source, quarantine)
                        self.tx(lambda db: db.execute("UPDATE blobs SET state='QUARANTINED' WHERE hash=?", (blob["hash"],)))
                        continue
                    quarantine.unlink(missing_ok=True)
                    self.tx(lambda db: db.execute("UPDATE blobs SET state='DELETED' WHERE hash=?", (blob["hash"],)))
        return result

    def backup(self, destination):
        target = beneath(self.root, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        with self.lock(), closing_connection(self.path) as source:
            with sqlite3.connect(target) as dest:
                source.backup(dest)
        return target

    def checkpoint(self, truncate=False):
        with closing_connection(self.path) as db:
            return list(db.execute("PRAGMA wal_checkpoint(" + ("TRUNCATE" if truncate else "PASSIVE") + ")").fetchone())


def parquet_bytes(frame):
    import pyarrow as pa
    import pyarrow.parquet as pq
    frame = frame.copy()
    for col in frame.columns:
        if str(frame[col].dtype).startswith("datetime64"):
            import pandas as pd
            frame[col] = pd.to_datetime(frame[col], utc=True)
    frame = frame.reindex(sorted(frame.columns), axis=1)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    schema = table.schema.remove_metadata()
    sh = hashlib.sha256(schema.serialize().to_pybytes()).hexdigest()
    table = table.replace_schema_metadata({b"schema_version": b"3", b"schema_hash": sh.encode()})
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf, compression="zstd", row_group_size=122880)
    return buf.getvalue().to_pybytes()


def project_root(output):
    path = Path(output).resolve()
    for candidate in (path, *path.parents):
        if candidate.name == "outputs":
            return candidate.parent
    # Explicit output roots (including tests) retain isolated storage state.
    return path.parent
