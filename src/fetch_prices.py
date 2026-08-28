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


def parse_daily_bars(payload: dict) -> Dict[str, Tuple[float, Optional[float]]]:
    """bars/dailyレスポンスを {code: (調整後終値, 時価総額[円])} にする。

    MktCapは百万円単位で来るので円に揃える(financialsと同じ単位)。
    """
    out: Dict[str, Tuple[float, Optional[float]]] = {}
    for q in payload.get("data", []):
        code = q.get("Code")
        close = q.get("AdjC") if q.get("AdjC") is not None else q.get("C")
        if not code or close is None:
            continue
        mktcap = q.get("MktCap")
        out[from_jquants_code(str(code))] = (
            float(close), float(mktcap) * 1e6 if mktcap is not None else None)
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
    with conn:
        for code, (close, mktcap) in quotes.items():
            conn.execute(
                "INSERT OR REPLACE INTO prices(code, date, close, mktcap, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)", (code, day.isoformat(), close, mktcap, now))
    print(f"終値・時価総額取得: {len(quotes)}銘柄 ({day} 時点、無料プランは12週遅延)")


if __name__ == "__main__":
    main()
