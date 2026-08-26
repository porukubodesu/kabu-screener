"""IR BANKから財務CSVと大株主ページを取得してDBに格納する。

IR BANKは個人運営サイトで、負荷が高いと「表示制限中」ページ(302リダイレクト)を
返す。このスクリプトは:
- CSV(f.irbank.net)は既定12秒間隔、HTMLページは既定3秒間隔でアクセス
- 表示制限を検知したら既定30分待って同じ銘柄から再開(連続24回で諦めて終了)
- 制限ページや不正な中身は保存しない(生データは data/raw/{code}/ に保存)
- 取得済み銘柄はスキップ(fetch_logで管理)、--stale-days で古いものだけ再取得

使い方:
  .venv/bin/python -m src.fetch_irbank --limit 30          # 未取得から30社
  .venv/bin/python -m src.fetch_irbank --codes 7203,9983   # 指定銘柄(強制再取得)
  .venv/bin/python -m src.fetch_irbank                     # 未取得ぜんぶ(バックフィル)
  .venv/bin/python -m src.fetch_irbank --stale-days 30 --limit 100  # 日次ローリング更新
"""
import argparse
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from .db import DATA_DIR, get_conn
from .parse_irbank import parse_fy_csv, parse_holder_html

FY_CSV_URL = "https://f.irbank.net/files/{code}/fy-data-all.csv"
HOLDER_URL = "https://irbank.net/{code}/holder"
USER_AGENT = "kabu-screener-personal/0.1 (personal use; contact: porukubodesu@gmail.com)"
BLOCK_MARKER = "表示制限"

FIN_FIELDS = [
    "revenue", "op_income", "ordinary_income", "net_income", "eps", "roe", "roa",
    "total_assets", "net_assets", "shareholders_equity", "retained_earnings",
    "bps", "equity_ratio",
    "op_cf", "inv_cf", "fin_cf", "capex", "cash", "op_cf_margin",
    "dividend", "buyback", "payout_ratio", "total_return_ratio",
]


class RateLimited(Exception):
    """IR BANKの「表示制限中」ページを検知した。"""


def _get(session: requests.Session, url: str, sleep: float) -> Optional[bytes]:
    """404はNone。5xx/接続エラーは2回リトライ。"""
    last_err = None
    for attempt in range(3):
        time.sleep(sleep if attempt == 0 else sleep * (attempt + 1))
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as e:
            last_err = e
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            continue
        resp.raise_for_status()
        return resp.content
    raise RuntimeError(f"fetch failed after retries: {url} ({last_err})")


def fetch_csv(session, code: str, sleep: float) -> Optional[bytes]:
    content = _get(session, FY_CSV_URL.format(code=code), sleep)
    if content is None:
        return None
    head = content[:1000].decode("utf-8-sig", errors="replace")
    if BLOCK_MARKER in head:
        raise RateLimited(f"csv blocked: {code}")
    if head.lstrip().startswith("<"):
        # 制限以外の理由でHTMLが返った(想定外)。保存せずスキップ
        return None
    return content


def fetch_holder(session, code: str, sleep: float) -> Optional[bytes]:
    content = _get(session, HOLDER_URL.format(code=code), sleep)
    if content is None:
        return None
    head = content[:2000].decode("utf-8", errors="replace")
    if BLOCK_MARKER in head:
        raise RateLimited(f"holder blocked: {code}")
    return content


def has_edinet_financials(conn, code: str) -> bool:
    """EDINET取り込み済みか。済みならEDINETを正とし、IR BANKの財務は使わない
    (有報サマリー5年分の方が網羅的。大株主は時点が違うだけなので併記でよい)。"""
    return conn.execute(
        "SELECT 1 FROM financials WHERE code = ? AND source = 'edinet' LIMIT 1",
        (code,)).fetchone() is not None


def save_company(conn, code: str, csv_bytes: Optional[bytes], holder_bytes: Optional[bytes]):
    raw_dir = DATA_DIR / "raw" / code
    raw_dir.mkdir(parents=True, exist_ok=True)

    fin_count = holder_count = 0
    with conn:
        if csv_bytes and not has_edinet_financials(conn, code):
            (raw_dir / "fy-data-all.csv").write_bytes(csv_bytes)
            fin_rows = parse_fy_csv(csv_bytes.decode("utf-8-sig", errors="replace"))
            if fin_rows:
                # 再取得時、収録期間から外れた古い年度が残らないよう入れ替える
                conn.execute("DELETE FROM financials WHERE code = ?", (code,))
            for row in fin_rows:
                values = [row.get(f) for f in FIN_FIELDS]
                conn.execute(
                    f"""INSERT OR REPLACE INTO financials
                        (code, fiscal_year, is_forecast, {', '.join(FIN_FIELDS)})
                        VALUES (?, ?, ?, {', '.join('?' * len(FIN_FIELDS))})""",
                    [code, row["fiscal_year"], row["is_forecast"]] + values,
                )
            fin_count = len(fin_rows)
        if holder_bytes:
            (raw_dir / "holder.html").write_bytes(holder_bytes)
            holder_rows = parse_holder_html(holder_bytes.decode("utf-8", errors="replace"))
            if holder_rows:
                # 再取得時の残留行は時点単位で入れ替えて消す。銘柄単位で消すと
                # EDINETだけが持つ時点のスナップショットまで巻き添えになる
                for as_of in {h["as_of"] for h in holder_rows}:
                    conn.execute(
                        "DELETE FROM holders WHERE code = ? AND as_of = ?",
                        (code, as_of))
            for h in holder_rows:
                conn.execute(
                    """INSERT OR REPLACE INTO holders(code, as_of, rank, holder_name, ratio)
                       VALUES (?, ?, ?, ?, ?)""",
                    (code, h["as_of"], h["rank"], h["holder_name"], h["ratio"]),
                )
            holder_count = len(holder_rows)

        status = "ok" if (fin_count or holder_count) else "no_data"
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log(code, fetched_at, status) VALUES (?, ?, ?)",
            (code, datetime.now().isoformat(timespec="seconds"), status),
        )
    return fin_count, holder_count


def pick_targets(conn, codes, limit, stale_days):
    if codes:
        return [c.strip() for c in codes.split(",") if c.strip()]
    if stale_days is not None:
        cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat(timespec="seconds")
        sql = """SELECT c.code FROM companies c
                 LEFT JOIN fetch_log f ON f.code = c.code
                 WHERE f.code IS NULL OR f.fetched_at < ? OR f.status LIKE 'error%'
                 ORDER BY f.fetched_at IS NOT NULL, f.fetched_at, c.code"""
        rows = conn.execute(sql, (cutoff,)).fetchall()
    else:
        sql = """SELECT c.code FROM companies c
                 LEFT JOIN fetch_log f ON f.code = c.code
                 WHERE f.code IS NULL ORDER BY c.code"""
        rows = conn.execute(sql).fetchall()
    targets = [r["code"] for r in rows]
    return targets[:limit] if limit else targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", help="カンマ区切りの証券コード(指定時は強制再取得)")
    ap.add_argument("--limit", type=int, help="最大取得社数")
    ap.add_argument("--stale-days", type=int, help="この日数より古い取得分も対象にする")
    ap.add_argument("--csv-sleep", type=float, default=190.0,
                    help="CSV取得の間隔秒(f.irbank.netの実測クォータ約10件/30分に"
                         "合わせた既定190。これ未満に下げない)")
    ap.add_argument("--sleep", type=float, default=3.0, help="HTMLページ取得の間隔秒(既定3)")
    ap.add_argument("--block-wait", type=float, default=1800,
                    help="表示制限検知時の待機秒(既定1800=30分)")
    ap.add_argument("--max-blocks", type=int, default=24,
                    help="連続でこの回数制限されたら諦めて終了(既定24)")
    args = ap.parse_args()

    conn = get_conn()
    targets = pick_targets(conn, args.codes, args.limit, args.stale_days)
    if not targets:
        print("対象なし(全銘柄取得済み)。--stale-days で再取得できます")
        return

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    # EDINET取り込み済みの銘柄は財務CSVをスキップする(ループ内参照)ので、
    # CSVの重い間隔(190s)はスキップされない銘柄数にだけ掛けて見積もる
    n_csv = sum(1 for c in targets if not has_edinet_financials(conn, c))
    est_h = (n_csv * args.csv_sleep + len(targets) * (args.sleep + 1)) / 3600
    print(f"{len(targets)}社を取得します(うち財務CSVあり{n_csv}社、"
          f"csv間隔{args.csv_sleep}s、推定{est_h:.1f}時間+制限待ち)", flush=True)

    ok = no_data = errors = 0
    blocked_streak = 0
    i = 0
    while i < len(targets):
        code = targets[i]
        try:
            # EDINET取り込み済みの銘柄は財務CSVを取得しない(捨てるデータのために
            # 制限が厳しいf.irbank.netのクォータを消費しないため)。大株主だけ更新
            if has_edinet_financials(conn, code):
                csv_bytes = None
            else:
                csv_bytes = fetch_csv(session, code, args.csv_sleep)
            holder_bytes = fetch_holder(session, code, args.sleep)
        except RateLimited as e:
            blocked_streak += 1
            if blocked_streak >= args.max_blocks:
                print(f"表示制限が{blocked_streak}回連続。中断します(再実行で続きから)", flush=True)
                break
            print(f"[{datetime.now():%H:%M}] 表示制限を検知({e})。{args.block_wait / 60:.0f}分待機",
                  flush=True)
            time.sleep(args.block_wait)
            continue  # 同じ銘柄からやり直し
        except Exception as e:
            errors += 1
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO fetch_log(code, fetched_at, status) VALUES (?, ?, ?)",
                    (code, datetime.now().isoformat(timespec="seconds"), f"error:{e}"),
                )
            i += 1
            continue

        blocked_streak = 0
        fin_count, holder_count = save_company(conn, code, csv_bytes, holder_bytes)
        if fin_count or holder_count:
            ok += 1
        else:
            no_data += 1
        i += 1
        if i % 50 == 0 or i == len(targets):
            print(f"  [{datetime.now():%H:%M}] {i}/{len(targets)} 完了 "
                  f"(ok={ok}, no_data={no_data}, error={errors})", flush=True)

    print(f"取得完了: ok={ok}, no_data={no_data}, error={errors}")


if __name__ == "__main__":
    main()
