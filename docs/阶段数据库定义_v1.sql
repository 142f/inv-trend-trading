-- 实际执行DDL的审计导出；不得另建独立权威数据库。

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
