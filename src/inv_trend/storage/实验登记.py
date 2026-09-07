"""Single SQLite writer for experiment facts and immutable journal events."""
from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path

from .基础 import encoded, now, redact
from .仓库 import Storage, project_root, StorageIntegrityError


class SQLiteRegistry:
    def __init__(self, path, *, root=None):
        self.path = Path(path).resolve()
        self.binding = self.path.with_name(self.path.name + ".存储指针.json")
        resolved = Path(root).resolve() if root is not None else None
        if self.binding.exists():
            binding = json.loads(self.binding.read_text(encoding="utf-8"))
            selected = (self.binding.parent / binding["root_relative"]).resolve()
            if resolved is not None and resolved != selected:
                raise StorageIntegrityError("registry root conflicts with its stored binding")
            resolved = selected
        self.store = Storage(resolved if resolved is not None else project_root(self.path))
        self.registry_id = str(self.path)
        from .基础 import atomic_write
        binding = {"schema_version":1, "root_relative":os.path.relpath(self.store.root,self.binding.parent)}
        with self.store.lock():
            if self.binding.exists():
                if json.loads(self.binding.read_text(encoding="utf-8")) != binding:
                    raise StorageIntegrityError("registry binding conflict")
            else:
                atomic_write(self.binding, encoded(binding))

    def read(self):
        rows = self.store.rows("SELECT payload_json FROM experiment_events WHERE registry_id=? ORDER BY sequence", (self.registry_id,))
        values = [json.loads(r["payload_json"]) for r in rows]
        previous = ""
        for row in values:
            payload = {k:v for k,v in row.items() if k != "event_hash"}
            expected = hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,
                allow_nan=False,default=str).encode()).hexdigest()
            if row.get("previous_hash") != previous or row.get("event_hash") != expected:
                raise StorageIntegrityError("experiment journal integrity failure")
            previous = row["event_hash"]
        return values

    def append(self, event, digest):
        event = redact(event)
        def write(db):
            last = db.execute("SELECT event_hash FROM experiment_events WHERE registry_id=? ORDER BY sequence DESC LIMIT 1", (self.registry_id,)).fetchone()
            row = dict(event, recorded_at=now(), previous_hash=last[0] if last else "")
            row["event_hash"] = digest(row)
            self._insert(db, row)
            return row
        return self.store.tx(write)

    def _insert(self, db, row):
        rid = row["run_id"]
        if row["event"] == "START":
            db.execute("INSERT INTO runs(run_id,task,report_date,created_at,config_hash,code_hash) VALUES(?,'experiment_registry',?,?,?,?)",
                       (rid, row["recorded_at"][:10], row["recorded_at"], row.get("spec_hash"), row.get("code_hash")))
            db.execute("INSERT INTO experiments(experiment_id,run_id,sample_status,kind,parent_id,trial_id,status,payload_json) VALUES(?,?,?,?,?,?,'RUNNING',?)",
                       (rid, rid, row["sample_status"], row.get("kind", "RESEARCH"), row.get("parent"), row.get("trial_id"), encoded(row).decode()))
        elif row["event"] == "FINISH":
            old = db.execute("SELECT status FROM experiments WHERE experiment_id=?", (rid,)).fetchone()
            if not old:
                raise StorageIntegrityError("FINISH has no START")
            if old[0] != "RUNNING":
                raise StorageIntegrityError("experiment already finished")
            db.execute("UPDATE experiments SET status=?,payload_json=? WHERE experiment_id=?", (row["status"], encoded(row).decode(), rid))
            db.execute("UPDATE runs SET status=? WHERE run_id=?", (row["status"], rid))
        else:
            raise ValueError("unknown registry event")
        db.execute("INSERT INTO experiment_events(registry_id,event_hash,previous_hash,payload_json) VALUES(?,?,?,?)",
                   (self.registry_id, row["event_hash"], row["previous_hash"], encoded(row).decode()))

    def import_jsonl(self, digest):
        # Validate the entire chain BEFORE the single import transaction.
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            value = {k: v for k, v in row.items() if k != "event_hash"}
            if row["event_hash"] != digest(value) or row["previous_hash"] != (rows[-1]["event_hash"] if rows else ""):
                raise StorageIntegrityError("experiment journal integrity failure")
            if redact(row) != row:
                raise StorageIntegrityError("journal contains secrets; redact through an explicit provenance migration")
            rows.append(row)
        def write(db):
            existing = [r[0] for r in db.execute("SELECT event_hash FROM experiment_events WHERE registry_id=? ORDER BY sequence", (self.registry_id,))]
            hashes = [r["event_hash"] for r in rows]
            if existing:
                if existing[:len(hashes)] != hashes:
                    raise StorageIntegrityError("journal conflicts with authoritative registry")
                return
            for row in rows:
                self._insert(db, row)
        self.store.tx(write)
        return len(rows)
