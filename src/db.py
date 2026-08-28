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
    source          TEXT NOT NULL DEFAULT 'irbank',  -- 'irbank' / 'edinet'
    shares_issued   INTEGER,        -- 発行済株式総数(EDINETのみ。希薄化計算用)
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

-- EDINETから取り込んだ書類の記録(fetch_logはIR BANK用なので分ける)。
-- 差分更新のスキップ判定と、訂正報告書の追跡に使う
CREATE TABLE IF NOT EXISTS edinet_docs (
    doc_id        TEXT PRIMARY KEY, -- 書類管理番号('S100XXXX')
    code          TEXT NOT NULL,
    doc_type_code TEXT NOT NULL,    -- '120'=有報
    period_end    TEXT,             -- 'YYYY-MM-DD'
    submitted_at  TEXT,
    fetched_at    TEXT NOT NULL,
    status        TEXT NOT NULL     -- 'ok' / 'no_data' / 'error:...'
);

-- 有報「事業の内容」の全文(EDINET由来、HTML除去済み)。
-- スクリーニング時のNGワード除外(screen.NG_BUSINESS_KEYWORDS)と表示に使う
CREATE TABLE IF NOT EXISTS business (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    period_end  TEXT,             -- 由来した有報の期末('YYYY-MM-DD')
    updated_at  TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS prices (
    code       TEXT PRIMARY KEY,
    date       TEXT NOT NULL,      -- 終値の日付('YYYY-MM-DD')
    close      REAL NOT NULL,      -- 調整後終値(円)
    mktcap     REAL,               -- 時価総額(円。J-Quants MktCap由来)
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bars_backfill (
    code    TEXT PRIMARY KEY,   -- 日足2年バックフィル済みの銘柄(新規上場の再取得防止)
    done_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_bars (
    code  TEXT NOT NULL,
    date  TEXT NOT NULL,           -- 'YYYY-MM-DD'
    open  REAL, high REAL, low REAL, close REAL,   -- 調整後
    PRIMARY KEY (code, date)
);

CREATE INDEX IF NOT EXISTS idx_holders_code ON holders(code, as_of);
CREATE INDEX IF NOT EXISTS idx_financials_code ON financials(code);
"""


def _migrate(conn):
    """既存DBに後から増えた列を足す(CREATE IF NOT EXISTSでは列が増えないため)。"""
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(prices)")}
    if pcols and "mktcap" not in pcols:
        conn.execute("ALTER TABLE prices ADD COLUMN mktcap REAL")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(financials)")}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE financials ADD COLUMN source TEXT NOT NULL DEFAULT 'irbank'")
    if "shares_issued" not in cols:
        conn.execute("ALTER TABLE financials ADD COLUMN shares_issued INTEGER")


def get_conn(db_path=DB_PATH):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    with conn:
        _migrate(conn)
    return conn
