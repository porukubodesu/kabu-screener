"""SQLiteスキーマとコネクション管理。"""
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    code       TEXT PRIMARY KEY,   -- 証券コード('7203', '130A'など英字含む)
    name       TEXT NOT NULL,
    market     TEXT NOT NULL,      -- プライム/スタンダード/グロース
    sector33   TEXT,
    scale      TEXT,               -- TOPIX規模区分
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financials (
    code            TEXT NOT NULL,
    fiscal_year     TEXT NOT NULL,  -- '2026/03'
    is_forecast     INTEGER NOT NULL DEFAULT 0,
    revenue         INTEGER,
    op_income       INTEGER,
    ordinary_income INTEGER,
    net_income      INTEGER,
    eps             REAL,
    roe             REAL,
    roa             REAL,
    total_assets    INTEGER,
    net_assets      INTEGER,
    shareholders_equity INTEGER,
    retained_earnings   INTEGER,
    bps             REAL,
    equity_ratio    REAL,
    op_cf           INTEGER,
    inv_cf          INTEGER,
    fin_cf          INTEGER,
    capex           INTEGER,
    cash            INTEGER,
    op_cf_margin    REAL,
    dividend        REAL,
    buyback         INTEGER,
    payout_ratio    REAL,
    total_return_ratio REAL,
    PRIMARY KEY (code, fiscal_year)
);

CREATE TABLE IF NOT EXISTS holders (
    code        TEXT NOT NULL,
    as_of       TEXT NOT NULL,      -- '2026/03'(半期ごとのスナップショット)
    rank        INTEGER NOT NULL,
    holder_name TEXT NOT NULL,
    ratio       REAL,               -- 保有比率(%)
    PRIMARY KEY (code, as_of, holder_name)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    code       TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    status     TEXT NOT NULL        -- 'ok' / 'no_data' / 'error:...'
);

CREATE TABLE IF NOT EXISTS screen_results (
    run_date     TEXT NOT NULL,     -- 'YYYY-MM-DD'
    code         TEXT NOT NULL,
    rank         INTEGER NOT NULL,
    score        REAL NOT NULL,
    metrics_json TEXT NOT NULL,
    notified_at  TEXT,
    PRIMARY KEY (run_date, code)
);

CREATE INDEX IF NOT EXISTS idx_holders_code ON holders(code, as_of);
CREATE INDEX IF NOT EXISTS idx_financials_code ON financials(code);
"""


def get_conn(db_path=DB_PATH):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
