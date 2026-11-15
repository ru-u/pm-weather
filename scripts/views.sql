-- DuckDB view layer over logger output. Usage:
--   duckdb -cmd ".read scripts/views.sql"          (from the repo root)
-- Point ROOT at data (local logger) or data/bucket (after `make pull-data`).
SET TimeZone = 'UTC';  -- every timestamp is stored in UTC; do not let the session localise them
SET VARIABLE root = 'data';

CREATE OR REPLACE VIEW orderbooks AS
  SELECT * FROM read_parquet(getvariable('root') || '/live/kalshi/orderbooks/*/*.parquet', hive_partitioning = true);

CREATE OR REPLACE VIEW markets AS
  SELECT * FROM read_parquet(getvariable('root') || '/live/kalshi/markets/*/*.parquet', hive_partitioning = true);

-- Trades are re-pulled with overlap on purpose; one row per trade_id is the truth.
CREATE OR REPLACE VIEW trades AS
  SELECT * FROM read_parquet(getvariable('root') || '/live/kalshi/trades/*/*.parquet', hive_partitioning = true)
  QUALIFY row_number() OVER (PARTITION BY trade_id ORDER BY received_at_utc) = 1;

-- Latest metadata version per market; market_meta_history keeps every version.
CREATE OR REPLACE VIEW market_meta_history AS
  SELECT * FROM read_parquet(getvariable('root') || '/live/kalshi/market_meta/*/*.parquet', hive_partitioning = true)
  QUALIFY row_number() OVER (PARTITION BY ticker, meta_sha256 ORDER BY received_at_utc) = 1;

CREATE OR REPLACE VIEW market_meta AS
  SELECT * FROM market_meta_history
  QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY received_at_utc DESC) = 1;

CREATE OR REPLACE VIEW health AS
  SELECT * FROM read_parquet(getvariable('root') || '/live/pmw/health/*/*.parquet', hive_partitioning = true);

-- Top of book joined to strike metadata: the shape most analyses start from.
CREATE OR REPLACE VIEW quotes AS
  SELECT o.received_at_utc, o.ticker, o.event_ticker, o.series_ticker, o.grp, o.trigger,
         m.strike_type, m.floor_strike, m.cap_strike, m.close_time,
         o.best_yes_bid, o.best_yes_bid_qty, o.best_yes_ask, o.best_yes_ask_qty,
         o.best_yes_ask - o.best_yes_bid AS spread
  FROM orderbooks o LEFT JOIN market_meta m USING (ticker);
