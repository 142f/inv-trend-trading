CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, task TEXT NOT NULL, report_date TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'RUNNING', phase TEXT NOT NULL DEFAULT 'STAGING',
 manifest_hash TEXT, manifest_path TEXT, config_hash TEXT, code_hash TEXT,
 created_at TEXT NOT NULL, committed_at TEXT, commit_seq INTEGER UNIQUE,
 retired_at TEXT, integrity_error TEXT,
 CHECK(phase IN ('STAGING','FILES_PUBLISHED','DB_COMMITTED','RUN_SUCCEEDED','LATEST_UPDATED')));
CREATE INDEX IF NOT EXISTS runs_query ON runs(task,status,commit_seq);
CREATE TABLE IF NOT EXISTS parameter_sets(parameter_hash TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parameter_values(parameter_hash TEXT NOT NULL REFERENCES parameter_sets, name TEXT NOT NULL, value_type TEXT NOT NULL, number_value REAL, text_value TEXT, PRIMARY KEY(parameter_hash,name));
CREATE INDEX IF NOT EXISTS parameter_query ON parameter_values(name,number_value,text_value);
CREATE TABLE IF NOT EXISTS scenarios(scenario_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments(
 experiment_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
 combination_id TEXT, parameter_hash TEXT REFERENCES parameter_sets,
 sample_status TEXT NOT NULL DEFAULT 'UNKNOWN', kind TEXT NOT NULL DEFAULT 'RESEARCH',
 parent_id TEXT, trial_id TEXT, assumptions_hash TEXT, result_hash TEXT,
 status TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS experiments_query ON experiments(run_id,trial_id,status);
CREATE TABLE IF NOT EXISTS experiment_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, registry_id TEXT NOT NULL,
 event_hash TEXT NOT NULL UNIQUE, previous_hash TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON experiment_events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON experiment_events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TABLE IF NOT EXISTS metrics(
 experiment_id TEXT NOT NULL REFERENCES experiments, instrument TEXT NOT NULL,
 sample_role TEXT NOT NULL, window_id TEXT NOT NULL, fold_id TEXT NOT NULL,
 scenario_id TEXT NOT NULL REFERENCES scenarios, metric_name TEXT NOT NULL, value REAL,
 PRIMARY KEY(experiment_id,instrument,sample_role,window_id,fold_id,scenario_id,metric_name));
CREATE INDEX IF NOT EXISTS metrics_query ON metrics(metric_name,sample_role,scenario_id,value);
CREATE TABLE IF NOT EXISTS dataset_refs(dataset_id TEXT PRIMARY KEY, version TEXT NOT NULL, checksum TEXT NOT NULL, manifest_path TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_inputs(run_id TEXT NOT NULL REFERENCES runs,dataset_id TEXT NOT NULL REFERENCES dataset_refs,role TEXT NOT NULL,PRIMARY KEY(run_id,dataset_id,role));
CREATE TABLE IF NOT EXISTS blobs(
 hash TEXT PRIMARY KEY CHECK(length(hash)=64), path TEXT NOT NULL UNIQUE,
 size INTEGER NOT NULL, format TEXT NOT NULL, schema_version TEXT, schema_hash TEXT,
 created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'LIVE', quarantine_path TEXT, quarantined_at TEXT);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY,blob_hash TEXT NOT NULL REFERENCES blobs,kind TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 3);
CREATE TABLE IF NOT EXISTS retention_roots(
 root_id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs, experiment_id TEXT REFERENCES experiments,
 retention_class TEXT NOT NULL CHECK(retention_class IN ('RAW','BASELINE','AUDIT','PAPER_FORWARD','RESULT','MIGRATION_SOURCE','CACHE','TEMP')),
 protection_reason TEXT NOT NULL, retain_until TEXT, retired_at TEXT);
CREATE TABLE IF NOT EXISTS artifact_refs(
 root_id TEXT NOT NULL REFERENCES retention_roots,artifact_id TEXT NOT NULL REFERENCES artifacts,
 logical_name TEXT NOT NULL,PRIMARY KEY(root_id,logical_name));
CREATE INDEX IF NOT EXISTS refs_blob ON artifacts(blob_hash);
CREATE TABLE IF NOT EXISTS signal_protections(signal_key TEXT PRIMARY KEY,root_id TEXT NOT NULL REFERENCES retention_roots,receipt TEXT);
CREATE TABLE IF NOT EXISTS publication_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES runs,phase TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(run_id TEXT PRIMARY KEY REFERENCES runs,path TEXT NOT NULL,token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_items(
 migration_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, action TEXT NOT NULL,
 source_size INTEGER NOT NULL, source_hash TEXT NOT NULL, target_hash TEXT REFERENCES blobs,
 precondition_hash TEXT NOT NULL, quarantine_path TEXT, rollback_status TEXT,
 state TEXT NOT NULL CHECK(state IN ('DISCOVERED','PLANNED','COPIED','VERIFIED','CUTOVER','QUARANTINED','DELETED','FAILED','ROLLED_BACK')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, cutover_cycle INTEGER, quarantined_at TEXT);
CREATE TABLE IF NOT EXISTS production_cycles(cycle_id TEXT PRIMARY KEY,daily_run_id TEXT NOT NULL REFERENCES runs,completed_at TEXT NOT NULL,evidence_json TEXT NOT NULL);
