-- 单一 SQLite 元数据权威；所有 UTC 时间为 ISO8601，保留来源字节用于审计。
CREATE TABLE IF NOT EXISTS store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS store_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_schemas(schema_id TEXT PRIMARY KEY,schema_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_objects(
 object_id TEXT PRIMARY KEY CHECK(length(object_id)=64),
 path TEXT NOT NULL UNIQUE, format TEXT NOT NULL, codec TEXT NOT NULL DEFAULT 'identity',
 original_size INTEGER NOT NULL CHECK(original_size>=0), stored_size INTEGER NOT NULL CHECK(stored_size>=0),
 stored_sha256 TEXT NOT NULL CHECK(length(stored_sha256)=64), stage TEXT NOT NULL,
 rows_count INTEGER, schema_id TEXT REFERENCES data_schemas(schema_id), created_at TEXT NOT NULL,
 CHECK(stage IN ('raw','processed','features','signals','backtests','reports','metadata'))
);
CREATE TABLE IF NOT EXISTS data_aliases(
 logical_path TEXT PRIMARY KEY,object_id TEXT NOT NULL REFERENCES data_objects(object_id),
 stage TEXT NOT NULL,source_size INTEGER NOT NULL,source_sha256 TEXT NOT NULL,
 instrument_id TEXT,timeframe TEXT,year INTEGER,dataset_version TEXT,
 purpose TEXT NOT NULL DEFAULT 'SOURCE',created_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS document_payloads(
 sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),payload BLOB NOT NULL,
 codec TEXT NOT NULL DEFAULT 'identity',byte_size INTEGER NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents(
 logical_path TEXT PRIMARY KEY,sha256 TEXT NOT NULL REFERENCES document_payloads(sha256),
 kind TEXT NOT NULL,run_id TEXT,dataset_version TEXT,symbol TEXT,timeframe TEXT,
 status TEXT,source_time TEXT,created_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS instruments_v3(
 instrument_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,asset_class TEXT NOT NULL,
 timezone TEXT NOT NULL DEFAULT 'UTC',source TEXT,metadata_status TEXT NOT NULL DEFAULT 'SOURCE_UNVERIFIED'
);
CREATE TABLE IF NOT EXISTS dataset_versions(
 version TEXT PRIMARY KEY,instrument_id TEXT NOT NULL REFERENCES instruments_v3(instrument_id),
 symbol TEXT NOT NULL,timeframe TEXT NOT NULL,channel TEXT NOT NULL,
 artifact_path TEXT NOT NULL REFERENCES data_aliases(logical_path),
 parent_version TEXT REFERENCES dataset_versions(version),publication_run_id TEXT,
 manifest_path TEXT REFERENCES documents(logical_path),quality_path TEXT REFERENCES documents(logical_path),
 row_count INTEGER,start_time TEXT,end_time TEXT,status TEXT NOT NULL,
 created_at TEXT NOT NULL,rule_version TEXT
);
CREATE TABLE IF NOT EXISTS dataset_heads(
 symbol TEXT NOT NULL,timeframe TEXT NOT NULL,channel TEXT NOT NULL,
 version TEXT NOT NULL REFERENCES dataset_versions(version),run_id TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,
 PRIMARY KEY(symbol,timeframe,channel)
);
CREATE TABLE IF NOT EXISTS quality_assessments(
 document_path TEXT PRIMARY KEY REFERENCES documents(logical_path),
 dataset_version TEXT,run_id TEXT,symbol TEXT,timeframe TEXT,status TEXT,
 actual_bars INTEGER,theoretical_bars INTEGER,missing_count INTEGER,missing_ratio REAL,
 duplicate_count INTEGER,ohlc_anomaly_count INTEGER,quality_score REAL,
 backtest_suitable INTEGER,calendar_valid INTEGER,timezone_valid INTEGER,
 source_time TEXT
);
CREATE TABLE IF NOT EXISTS quality_intervals(
 document_path TEXT NOT NULL REFERENCES quality_assessments(document_path),ordinal INTEGER NOT NULL,
 start_time TEXT,end_time TEXT,classification TEXT,expected_bars INTEGER,observed_bars INTEGER,
 calendar_id TEXT,PRIMARY KEY(document_path,ordinal)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lineage_links(
 document_path TEXT NOT NULL REFERENCES documents(logical_path),ordinal INTEGER NOT NULL,
 reference_path TEXT NOT NULL,expected_sha256 TEXT,actual_sha256 TEXT,
 object_id TEXT REFERENCES data_objects(object_id),target_document TEXT REFERENCES documents(logical_path),
 status TEXT NOT NULL,PRIMARY KEY(document_path,ordinal)
);
CREATE TABLE IF NOT EXISTS audit_events_v3(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,subject TEXT NOT NULL,
 occurred_at TEXT NOT NULL,payload_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS audit_v3_no_update BEFORE UPDATE ON audit_events_v3 BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_v3_no_delete BEFORE DELETE ON audit_events_v3 BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
CREATE TABLE IF NOT EXISTS source_inventory(
 source_path TEXT PRIMARY KEY,source_sha256 TEXT NOT NULL,source_size INTEGER NOT NULL,
 action TEXT NOT NULL,target_kind TEXT NOT NULL,target_id TEXT,verified INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_issues(
 issue_id INTEGER PRIMARY KEY AUTOINCREMENT,severity TEXT NOT NULL,category TEXT NOT NULL,
 source_path TEXT NOT NULL,details TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS retained_sources(
 source_name TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,scope TEXT NOT NULL,imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_runs(
 run_id TEXT PRIMARY KEY,logical_path TEXT NOT NULL UNIQUE,stage TEXT NOT NULL,status TEXT NOT NULL,
 strategy_version TEXT,code_hash TEXT,parameter_hash TEXT,data_version TEXT,scope TEXT,
 train_start TEXT,train_end TEXT,test_start TEXT,test_end TEXT,
 started_at TEXT,completed_at TEXT,metadata_path TEXT REFERENCES documents(logical_path),
 result_hash TEXT NOT NULL,parent_run_id TEXT REFERENCES result_runs(run_id)
);
CREATE TABLE IF NOT EXISTS result_parameters(
 parameter_hash TEXT PRIMARY KEY,payload_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_metrics(
 run_id TEXT NOT NULL REFERENCES result_runs(run_id),window_id TEXT NOT NULL DEFAULT '',
 instrument TEXT NOT NULL DEFAULT '',metric_name TEXT NOT NULL,value REAL,text_value TEXT,
 PRIMARY KEY(run_id,window_id,instrument,metric_name)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS result_tables(
 run_id TEXT NOT NULL REFERENCES result_runs(run_id),table_name TEXT NOT NULL,
 row_count INTEGER NOT NULL,schema_json TEXT NOT NULL,sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,table_name)
);
CREATE TABLE IF NOT EXISTS result_rows(
 run_id TEXT NOT NULL,table_name TEXT NOT NULL,ordinal INTEGER NOT NULL,
 event_time TEXT,instrument TEXT,side TEXT,pnl REAL,price REAL,quantity REAL,
 payload BLOB NOT NULL,codec TEXT NOT NULL,
 PRIMARY KEY(run_id,table_name,ordinal),
 FOREIGN KEY(run_id,table_name) REFERENCES result_tables(run_id,table_name)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS wf_windows_v3(
 study_id TEXT NOT NULL,iteration TEXT NOT NULL,window_id TEXT NOT NULL,
 train_start TEXT NOT NULL,train_end TEXT NOT NULL,test_start TEXT NOT NULL,test_end TEXT NOT NULL,
 candidate_id TEXT NOT NULL,parameter_hash TEXT NOT NULL REFERENCES result_parameters(parameter_hash),
 lock_hash TEXT NOT NULL,lock_document TEXT NOT NULL REFERENCES documents(logical_path),
 PRIMARY KEY(study_id,iteration,window_id),CHECK(train_end<test_start)
);
CREATE TABLE IF NOT EXISTS retention_pins(
 pin_id TEXT PRIMARY KEY,object_id TEXT REFERENCES data_objects(object_id),
 document_path TEXT REFERENCES documents(logical_path),reason TEXT NOT NULL,
 retained_until TEXT,retired_at TEXT,CHECK(object_id IS NOT NULL OR document_path IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS benchmark_rounds(
 round_id INTEGER PRIMARY KEY,design TEXT NOT NULL,source_hash TEXT NOT NULL,
 started_at TEXT NOT NULL,metrics_json TEXT NOT NULL,tests_json TEXT NOT NULL
);
-- 兼容 v2 的目录查询，不再保存独立的旧 Catalog 文件。
CREATE TABLE IF NOT EXISTS datasets(
 run_id TEXT PRIMARY KEY,symbol TEXT,instrument_id TEXT,timeframe TEXT,source TEXT,
 manifest_path TEXT,status TEXT,version TEXT,created_at TEXT,asset_class TEXT DEFAULT '',
 start_time TEXT DEFAULT '',end_time TEXT DEFAULT '',row_count INTEGER DEFAULT 0,
 schema_version TEXT DEFAULT '',checksum TEXT DEFAULT '',updated_at TEXT DEFAULT '',raw_source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS current_versions(
 symbol TEXT,timeframe TEXT,version TEXT,run_id TEXT,updated_at TEXT,PRIMARY KEY(symbol,timeframe)
);
CREATE TABLE IF NOT EXISTS rollbacks(
 rollback_id TEXT PRIMARY KEY,symbol TEXT,timeframe TEXT,previous_version TEXT,target_version TEXT,
 actor TEXT,reason TEXT,created_at TEXT
);
CREATE TABLE IF NOT EXISTS market_datasets_v3(
 dataset_id INTEGER PRIMARY KEY,dataset_hash TEXT NOT NULL UNIQUE,source_document TEXT NOT NULL REFERENCES documents(logical_path),
 row_count INTEGER NOT NULL,schema_json TEXT NOT NULL,frame_hash TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_bars_v3(
 dataset_id INTEGER NOT NULL REFERENCES market_datasets_v3(dataset_id),ordinal INTEGER NOT NULL,
 date TEXT NOT NULL,symbol TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,volume REAL,spread REAL,source TEXT,timeframe TEXT,
 PRIMARY KEY(dataset_id,ordinal),UNIQUE(dataset_id,symbol,date)
) WITHOUT ROWID;
-- UNIQUE(dataset_id,symbol,date) already provides the range-query index; do not duplicate it.
CREATE TABLE IF NOT EXISTS studies_v3(
 study_id TEXT PRIMARY KEY,protocol_document TEXT NOT NULL REFERENCES documents(logical_path),
 protocol_hash TEXT NOT NULL,data_version TEXT NOT NULL,code_hash TEXT,strategy_version TEXT,
 production_enabled INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,created_at TEXT NOT NULL
);

-- 所有研究、运行和训练内排名均通过外键关联，禁止用外层收益重写参数锁。
CREATE TABLE IF NOT EXISTS study_runs_v3(
 study_id TEXT NOT NULL REFERENCES studies_v3(study_id),
 run_id TEXT PRIMARY KEY REFERENCES result_runs(run_id),role TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS candidate_rankings_v3(
 study_id TEXT NOT NULL,iteration TEXT NOT NULL,window_id TEXT NOT NULL,candidate_id TEXT NOT NULL,
 score REAL,eligible INTEGER NOT NULL,positive_inner_ratio REAL,inner_drawdown REAL,
 trade_count INTEGER,complexity_rank INTEGER,
 PRIMARY KEY(study_id,iteration,window_id,candidate_id),
 FOREIGN KEY(study_id,iteration,window_id) REFERENCES wf_windows_v3(study_id,iteration,window_id)
) WITHOUT ROWID;
CREATE VIEW IF NOT EXISTS v_current_datasets AS
 SELECT h.symbol,h.timeframe,h.channel,h.version,h.revision,v.parent_version,v.status,v.row_count,
 v.start_time,v.end_time,v.artifact_path,o.path physical_path,o.object_id sha256,v.manifest_path,v.quality_path
 FROM dataset_heads h JOIN dataset_versions v USING(version)
 JOIN data_aliases a ON a.logical_path=v.artifact_path JOIN data_objects o USING(object_id);
CREATE VIEW IF NOT EXISTS v_backtest_summaries AS
 SELECT r.run_id,s.study_id,r.logical_path,r.status,r.strategy_version,r.scope,r.data_version,r.code_hash,
 r.parameter_hash,m.window_id,
 MAX(CASE WHEN m.metric_name='total_return' THEN m.value END) total_return,
 MAX(CASE WHEN m.metric_name='annualized_return' THEN m.value END) annualized_return,
 MAX(CASE WHEN m.metric_name='max_drawdown' THEN m.value END) max_drawdown,
 MAX(CASE WHEN m.metric_name='sharpe_ratio' THEN m.value END) sharpe_ratio,
 MAX(CASE WHEN m.metric_name='calmar_ratio' THEN m.value END) calmar_ratio,
 MAX(CASE WHEN m.metric_name='trade_count' THEN m.value END) trade_count,
 MAX(CASE WHEN m.metric_name='win_rate' THEN m.value END) win_rate,
 MAX(CASE WHEN m.metric_name='payoff_ratio' THEN m.value END) payoff_ratio,
 MAX(CASE WHEN m.metric_name='profit_factor' THEN m.value END) profit_factor,
 MAX(CASE WHEN m.metric_name='fee_cost' THEN m.value END) fee_cost,
 MAX(CASE WHEN m.metric_name='slippage_cost' THEN m.value END) slippage_cost,
 MAX(CASE WHEN m.metric_name='carry_cost' THEN m.value END) carry_cost
 FROM result_runs r LEFT JOIN study_runs_v3 s USING(run_id) LEFT JOIN result_metrics m USING(run_id)
 GROUP BY r.run_id,m.window_id;
CREATE VIEW IF NOT EXISTS v_window_parameters AS
 SELECT w.*,p.payload_json parameters_json,r.score,r.positive_inner_ratio,r.inner_drawdown,r.trade_count
 FROM wf_windows_v3 w JOIN result_parameters p ON w.parameter_hash=p.parameter_hash
 LEFT JOIN candidate_rankings_v3 r USING(study_id,iteration,window_id,candidate_id);
CREATE VIEW IF NOT EXISTS v_storage_usage AS
 SELECT stage,format,codec,count(*) physical_objects,sum(original_size) original_bytes,sum(stored_size) stored_bytes
 FROM data_objects GROUP BY stage,format,codec;
