CREATE TABLE IF NOT EXISTS live_markets (
  market_slug TEXT,
  condition_id TEXT,
  token_id TEXT,
  outcome_label TEXT,
  event_key TEXT,
  active INTEGER,
  closed INTEGER,
  resolved INTEGER,
  selected_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS live_orderbook_snapshots (
  snapshot_id TEXT,
  token_id TEXT,
  market_slug TEXT,
  condition_id TEXT,
  captured_at_utc TEXT,
  best_bid REAL,
  best_ask REAL,
  spread REAL,
  bid_depth_levels INTEGER,
  ask_depth_levels INTEGER,
  total_bid_shares REAL,
  total_ask_shares REAL,
  normalized_content_hash TEXT
);

CREATE TABLE IF NOT EXISTS live_vwap_validation (
  snapshot_id TEXT,
  token_id TEXT,
  action TEXT,
  intended_usd_or_shares REAL,
  filled_usd REAL,
  filled_shares REAL,
  vwap REAL,
  fully_filled INTEGER,
  calculation_valid INTEGER
);
