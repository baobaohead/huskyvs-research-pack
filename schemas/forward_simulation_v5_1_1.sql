PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS safe_audit (
  audit_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT NOT NULL,
  signal_hash TEXT NOT NULL,
  registration_audit_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  registered_at_utc TEXT NOT NULL,
  city TEXT NOT NULL,
  city_normalized TEXT NOT NULL,
  weather_date_local TEXT NOT NULL,
  weather_metric TEXT NOT NULL,
  event_key TEXT NOT NULL,
  market_slug TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  side TEXT NOT NULL,
  forecast_temperature TEXT,
  forecast_probability REAL,
  market_probability_at_signal REAL,
  intended_usd REAL NOT NULL,
  max_entry_price REAL NOT NULL,
  entry_deadline_utc TEXT NOT NULL,
  source TEXT NOT NULL,
  notes TEXT,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_order_state (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  intended_usd REAL NOT NULL,
  filled_entry_usd REAL NOT NULL,
  remaining_entry_usd REAL NOT NULL,
  filled_entry_shares REAL NOT NULL,
  entry_status TEXT NOT NULL,
  max_entry_price REAL NOT NULL,
  entry_deadline_utc TEXT NOT NULL,
  last_entry_attempt_at TEXT,
  last_attempt_reason TEXT,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT NOT NULL,
  captured_at_utc TEXT NOT NULL,
  token_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  raw_orderbook_json TEXT NOT NULL,
  source TEXT NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_fills (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_fill_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  token_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  filled_at_utc TEXT NOT NULL,
  gross_entry_cost REAL NOT NULL,
  entry_fee REAL NOT NULL,
  total_entry_cost REAL NOT NULL,
  filled_shares REAL NOT NULL,
  entry_vwap REAL NOT NULL,
  best_bid REAL,
  best_ask REAL,
  spread REAL,
  complete_fill INTEGER NOT NULL,
  unfilled_usd_after_fill REAL NOT NULL,
  depth_levels_json TEXT NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_lots (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  token_id TEXT NOT NULL,
  entry_fill_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  entry_shares REAL NOT NULL,
  gross_entry_cost REAL NOT NULL,
  entry_fee REAL NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_triggers (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  trigger_stage_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  token_id TEXT NOT NULL,
  trigger_created_at TEXT NOT NULL,
  trigger_target_shares REAL NOT NULL,
  trigger_filled_shares REAL NOT NULL,
  trigger_remaining_shares REAL NOT NULL,
  trigger_status TEXT NOT NULL,
  trigger_completed_at TEXT,
  rolling_avg_cost_at_trigger REAL NOT NULL,
  threshold_price REAL NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exit_fills (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  exit_fill_id TEXT NOT NULL,
  trigger_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  trigger_stage_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  token_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  filled_at_utc TEXT NOT NULL,
  planned_sell_shares REAL NOT NULL,
  filled_shares REAL NOT NULL,
  gross_exit_proceeds REAL NOT NULL,
  exit_fee REAL NOT NULL,
  net_exit_proceeds REAL NOT NULL,
  exit_vwap REAL NOT NULL,
  best_bid REAL,
  best_ask REAL,
  spread REAL,
  complete_fill INTEGER NOT NULL,
  unfilled_trigger_shares_after_fill REAL NOT NULL,
  depth_levels_json TEXT NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exit_fill_allocations (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  allocation_id TEXT NOT NULL,
  exit_fill_id TEXT NOT NULL,
  trigger_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  token_id TEXT NOT NULL,
  lot_id TEXT NOT NULL,
  allocated_shares REAL NOT NULL,
  gross_exit_proceeds REAL NOT NULL,
  exit_fee REAL NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  settlement_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source TEXT NOT NULL,
  source_reference TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL,
  raw_response TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  settlement_outcome TEXT NOT NULL,
  settlement_value REAL NOT NULL,
  operator_notes TEXT,
  settlement_status TEXT NOT NULL,
  remaining_shares_settled REAL NOT NULL,
  settlement_proceeds REAL NOT NULL,
  settlement_fee REAL NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_allocations (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  settlement_allocation_id TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  token_id TEXT NOT NULL,
  lot_id TEXT NOT NULL,
  settled_shares REAL NOT NULL,
  settlement_proceeds REAL NOT NULL,
  settlement_fee REAL NOT NULL,
  mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_results (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  signal_count INTEGER NOT NULL,
  position_count INTEGER NOT NULL,
  traded_event_count INTEGER NOT NULL,
  settled_event_count INTEGER NOT NULL,
  gross_entry_cost REAL NOT NULL,
  entry_fee REAL NOT NULL,
  gross_exit_proceeds REAL NOT NULL,
  exit_fee REAL NOT NULL,
  settlement_proceeds REAL NOT NULL,
  settlement_fee REAL NOT NULL,
  total_fees REAL NOT NULL,
  gross_pnl REAL,
  net_pnl REAL,
  triggered_take_profit INTEGER NOT NULL,
  incomplete_take_profit INTEGER NOT NULL
);
