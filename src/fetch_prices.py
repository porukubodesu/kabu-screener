"""J-Quants API v2 (JPX公式) から終値と時価総額を取る。

/v2/equities/bars/daily は date指定で全上場銘柄の四本値+時価総額(MktCap、百万円)を
返すため、遅延を考慮した直近の営業日を探して数リクエストで全銘柄分を保存する。
無料プランは12週遅延だが、時価総額の桁感の把握には十分。

必要な環境変数(.envに置く):
  JQUANTS_API_KEY   # https://jpx-jquants.com/ で無料登録 → ダッシュボードで発行

未設定なら何もせず正常終了する(daily.shを止めない)。
認証はヘッダ(x-api-key)なので、例外メッセージのURLにキーが混ざる漏えい経路はない。

使い方:
  .venv/bin/python -m src.fetch_prices
"""
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

import requests

from .db import get_conn

API = "https://api.jquants.com/v2"
FREE_PLAN_DELAY_DAYS = 12 * 7   # 無料プランの遅延(12週)


def to_jquants_code(code: str) -> str:
    """'7203' -> '72030' / '130A' -> '130A0' (J-Quantsは5桁コード)。"""
    return code + "0" if len(code) == 4 else code


def from_jquants_code(jq_code: str) -> str:
    return jq_code[:-1] if len(jq_code) == 5 and jq_code.endswith("0") else jq_code


def parse_daily_bars(payload: dict) -> Dict[str, tuple]:
    """bars/dailyレスポンスを {code: (終値, 時価総額[円], 始値, 高値, 安値)} にする。

    値は調整後(Adj系)を優先。MktCapは百万円単位で来るので円に揃える。
    """
    out: Dict[str, tuple] = {}
    for q in payload.get("data", []):
        code = q.get("Code")
        close = q.get("AdjC") if q.get("AdjC") is not None else q.get("C")
        if not code or close is None:
            continue
        mktcap = q.get("MktCap")
        o = q.get("AdjO") if q.get("AdjO") is not None else q.get("O")
        h = q.get("AdjH") if q.get("AdjH") is not None else q.get("H")
        l = q.get("AdjL") if q.get("AdjL") is not None else q.get("L")
        out[from_jquants_code(str(code))] = (
            float(close), float(mktcap) * 1e6 if mktcap is not None else None,
            o, h, l)
    return out


def fetch_day(session: requests.Session, api_key: str,
              day: date) -> Dict[str, Tuple[float, Optional[float]]]:
    """指定日の全銘柄分。ページネーション対応。データが無い日(休日)は空dict。"""
    quotes: Dict[str, Tuple[float, Optional[float]]] = {}
    key: Optional[str] = None
    while True:
        params = {"date": day.strftime("%Y%m%d")}
        if key:
            params["pagination_key"] = key
        r = session.get(f"{API}/equities/bars/daily", params=params,
                        headers={"x-api-key": api_key}, timeout=60)
        r.raise_for_status()
        payload = r.json()
        quotes.update(parse_daily_bars(payload))
        key = payload.get("pagination_key")
        if not key:
            return quotes


BACKFILL_SLEEP = 6.0    # 銘柄別バックフィルの間隔(無料プランのレート制限対策)
RATE_LIMIT_WAIT = 60.0  # 429時の待機


def _get_with_backoff(session, params: Dict, api_key: str):
    """429はレート制限なので待って再試行する(最大4回)。"""
    for attempt in range(4):
        r = session.get(f"{API}/equities/bars/daily", params=params,
                        headers={"x-api-key": api_key}, timeout=60)
        if r.status_code == 429:
            time.sleep(RATE_LIMIT_WAIT * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise requests.RequestException("rate limited repeatedly (429)")


def parse_bars(payload: dict):
    """bars/dailyレスポンスから日足(調整後OHLC)のリストを返す。"""
    out = []
    for q in payload.get("data", []):
        d = q.get("Date")
        o, h, l, c = (q.get("AdjO"), q.get("AdjH"), q.get("AdjL"), q.get("AdjC"))
        if c is None:
            o, h, l, c = (q.get("O"), q.get("H"), q.get("L"), q.get("C"))
        if d and c is not None:
            out.append((d, o, h, l, c))
    return out


def fetch_code_bars(session, api_key: str, code: str, frm: date, to: date):
    """1銘柄の日足履歴(調整後)。ページネーション対応。"""
    bars = []
    key: Optional[str] = None
    while True:
        params = {"code": to_jquants_code(code),
                  "from": frm.strftime("%Y%m%d"), "to": to.strftime("%Y%m%d")}
        if key:
            params["pagination_key"] = key
        payload = _get_with_backoff(session, params, api_key).json()
        bars.extend(parse_bars(payload))
        key = payload.get("pagination_key")
        if not key:
            return bars


def backfill_bars(conn, session, api_key: str, latest: date, top: int) -> None:
    """月足チャート用の過去2年ぶんを、履歴の無い銘柄だけ銘柄別に取得する。

    日々の新しい日足は main() が全銘柄一括(date指定)で追記するので、
    ここに来るのは表示上位に新しく入った銘柄などの初回だけ。無料プランの
    レート制限が厳しいため、間隔を空けて淡々と回す(429は待って再試行)。
    """
    # 完了マーカーの無い銘柄だけ対象にする。MIN(date)では新規上場銘柄
    # (履歴が2年に届かない)を「未取得」と誤判定し、毎日再取得してしまう
    done = {r["code"] for r in conn.execute("SELECT code FROM bars_backfill")}
    codes = [c for c in target_codes(conn, top) if c not in done]
    fetched = 0
    for code in codes:
        try:
            bars = fetch_code_bars(session, api_key, code,
                                   latest - timedelta(days=365 * 2), latest)
        except requests.RequestException as e:
            print(f"  [warn] 日足取得失敗({code}): {e}")
            continue
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO price_bars(code, date, open, high, low, close)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(code, d, o, h, l, c) for d, o, h, l, c in bars])
            conn.execute(
                "INSERT OR REPLACE INTO bars_backfill(code, done_at) VALUES (?, ?)",
                (code, datetime.now().isoformat(timespec="seconds")))
        fetched += 1
        time.sleep(BACKFILL_SLEEP)
    print(f"日足バックフィル: {fetched}/{len(codes)}銘柄")


def target_codes(conn, limit: int):
    run_date = conn.execute("SELECT MAX(run_date) FROM screen_results").fetchone()[0]
    if not run_date:
        return []
    return [r["code"] for r in conn.execute(
        "SELECT code FROM screen_results WHERE run_date = ? ORDER BY rank LIMIT ?",
        (run_date, limit))]


def main():
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("JQUANTS_API_KEY 未設定のため株価取得をスキップ"
              "(https://jpx-jquants.com/ で無料登録 → .env に追記)")
        return
    conn = get_conn()
    session = requests.Session()

    # 遅延ぶんを引いた日から過去へ、データのある営業日を探す(最大10日)
    day = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS)
    for _ in range(10):
        try:
            quotes = fetch_day(session, api_key, day)
        except requests.RequestException as e:
            sys.exit(f"bars/daily取得失敗({day}): {e}")
        if quotes:
            break
        day -= timedelta(days=1)
        time.sleep(1)
    else:
        sys.exit("直近10日分に株価データが見つかりません")

    now = datetime.now().isoformat(timespec="seconds")
    d = day.isoformat()
    with conn:
        for code, (close, mktcap, o, h, l) in quotes.items():
            conn.execute(
                "INSERT OR REPLACE INTO prices(code, date, close, mktcap, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)", (code, d, close, mktcap, now))
            # 同じレスポンスから日足も貯める(追加リクエストなしで履歴が伸びる)
            conn.execute(
                "INSERT OR REPLACE INTO price_bars(code, date, open, high, low, close)"
                " VALUES (?, ?, ?, ?, ?, ?)", (code, d, o, h, l, close))
    print(f"終値・時価総額取得: {len(quotes)}銘柄 ({day} 時点、無料プランは12週遅延)")

    # 月足チャート用の過去2年バックフィル(履歴の無い表示上位銘柄のみ)
    backfill_bars(conn, session, api_key, day, top=300)


if __name__ == "__main__":
    main()
