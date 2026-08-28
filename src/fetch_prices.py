"""J-Quants API (JPX公式) から終値を取り、時価総額の概算表示に使う。

無料プランは12週遅延だが、時価総額の桁感の把握には十分。
/prices/daily_quotes は date指定で全上場銘柄を1リクエストで返すため、
遅延を考慮した直近の営業日を探して1〜数リクエストで全銘柄分を保存する。

必要な環境変数(.envに置く):
  JQUANTS_MAIL / JQUANTS_PASSWORD   # https://jpx-jquants.com/ の無料登録アカウント

未設定なら何もせず正常終了する(daily.shを止めない)。

使い方:
  .venv/bin/python -m src.fetch_prices
"""
import os
import re
import sys
import time
from datetime import date, timedelta
from typing import Dict, List, Optional

import requests

from .db import get_conn

API = "https://api.jquants.com/v1"
FREE_PLAN_DELAY_DAYS = 12 * 7   # 無料プランの遅延(12週)

# requests例外の文字列には refreshtoken 入りの完全URLが含まれうるため、
# ログ・端末に出す前に必ず伏せる(fetch_edinetのAPIキーと同じ扱い)
_TOKEN_RE = re.compile(r"(refreshtoken=)[^&\s'\"]+", re.IGNORECASE)


def _redacted(err: object) -> str:
    return _TOKEN_RE.sub(r"\1***", str(err))


def to_jquants_code(code: str) -> str:
    """'7203' -> '72030' / '130A' -> '130A0' (J-Quantsは5桁コード)。"""
    return code + "0" if len(code) == 4 else code


def from_jquants_code(jq_code: str) -> str:
    return jq_code[:-1] if len(jq_code) == 5 and jq_code.endswith("0") else jq_code


def parse_daily_quotes(payload: dict) -> Dict[str, float]:
    """daily_quotesレスポンスを {code: 調整後終値} にする。"""
    out: Dict[str, float] = {}
    for q in payload.get("daily_quotes", []):
        close = q.get("AdjustmentClose") or q.get("Close")
        code = q.get("Code")
        if close is None or not code:
            continue
        out[from_jquants_code(str(code))] = float(close)
    return out


def get_id_token(session: requests.Session, mail: str, password: str) -> str:
    r = session.post(f"{API}/token/auth_user",
                     json={"mailaddress": mail, "password": password}, timeout=30)
    r.raise_for_status()
    refresh = r.json()["refreshToken"]
    r = session.post(f"{API}/token/auth_refresh",
                     params={"refreshtoken": refresh}, timeout=30)
    r.raise_for_status()
    return r.json()["idToken"]


def fetch_day(session: requests.Session, id_token: str, day: date) -> Dict[str, float]:
    """指定日の全銘柄終値。ページネーション対応。データが無い日(休日)は空dict。"""
    quotes: Dict[str, float] = {}
    key: Optional[str] = None
    while True:
        params = {"date": day.strftime("%Y%m%d")}
        if key:
            params["pagination_key"] = key
        r = session.get(f"{API}/prices/daily_quotes", params=params,
                        headers={"Authorization": f"Bearer {id_token}"}, timeout=60)
        r.raise_for_status()
        payload = r.json()
        quotes.update(parse_daily_quotes(payload))
        key = payload.get("pagination_key")
        if not key:
            return quotes


def main():
    mail = os.environ.get("JQUANTS_MAIL")
    password = os.environ.get("JQUANTS_PASSWORD")
    if not mail or not password:
        print("JQUANTS_MAIL / JQUANTS_PASSWORD 未設定のため株価取得をスキップ"
              "(https://jpx-jquants.com/ で無料登録 → .env に追記)")
        return
    conn = get_conn()
    session = requests.Session()
    try:
        id_token = get_id_token(session, mail, password)
    except requests.RequestException as e:
        sys.exit(f"J-Quants認証失敗: {_redacted(e)}")

    # 遅延ぶんを引いた日から過去へ、データのある営業日を探す(最大10日)
    day = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS)
    for _ in range(10):
        try:
            quotes = fetch_day(session, id_token, day)
        except requests.RequestException as e:
            sys.exit(f"daily_quotes取得失敗({day}): {_redacted(e)}")
        if quotes:
            break
        day -= timedelta(days=1)
        time.sleep(1)
    else:
        sys.exit("直近10日分に株価データが見つかりません")

    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        for code, close in quotes.items():
            conn.execute(
                "INSERT OR REPLACE INTO prices(code, date, close, fetched_at)"
                " VALUES (?, ?, ?, ?)", (code, day.isoformat(), close, now))
    print(f"終値取得: {len(quotes)}銘柄 ({day} 時点、無料プランは12週遅延)")


if __name__ == "__main__":
    main()
