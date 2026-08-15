"""EDINET API v2 から有価証券報告書(CSV)を取得してDBに格納する。

金融庁の公式API。APIキー(無料)が必要:
  EDINETトップページ https://disclosure2.edinet-fsa.go.jp/ の「EDINET API」から
  ユーザー登録 → マイページでAPIキー発行 → export EDINET_API_KEY=...

流れ:
- 書類一覧API(日付ごと)で有報(docTypeCode=120)を探し、上場銘柄のものだけ
  書類取得API(type=5, CSV)で取得 → parse_edinet でパース → DB格納
- 取り込んだ書類は edinet_docs に記録し、次回以降スキップ(差分更新)
- 有報の「主要な経営指標等の推移」は5年分あるので、1書類で5年度分が埋まる
- IR BANK由来の財務行とは年度単位でマージ(EDINET優先、単位はどちらも円)。
  EDINET取り込み済みの銘柄はfetch_irbank側が財務CSVの取得をスキップする

使い方:
  .venv/bin/python -m src.fetch_edinet --days 400          # 初回(直近400日の有報を走査)
  .venv/bin/python -m src.fetch_edinet --days 7            # 日次差分(cron向け)
  .venv/bin/python -m src.fetch_edinet --days 400 --codes 7203,9983
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

from .db import DATA_DIR, get_conn
from .fetch_irbank import FIN_FIELDS, USER_AGENT
from .parse_edinet import (parse_business, parse_financials, parse_holders,
                           read_jpcrp_rows)

API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
DOC_TYPE_ANNUAL = "120"  # 有価証券報告書(訂正=130は当面対象外)

EDINET_FIN_FIELDS = FIN_FIELDS + ["shares_issued"]

# 既存行が実績(IR BANK由来)ならEDINETに無いフィールドを温存(COALESCE)、
# 予想行なら総入れ替え(予想値が実績としてすり替わるのを防ぐ)。
# DO UPDATE SET の右辺は更新前の行を参照するので、is_forecastは元の値で分岐できる
_UPSERT_SQL = f"""
    INSERT INTO financials (code, fiscal_year, is_forecast, source,
                            {', '.join(EDINET_FIN_FIELDS)})
    VALUES (?, ?, 0, 'edinet', {', '.join('?' * len(EDINET_FIN_FIELDS))})
    ON CONFLICT(code, fiscal_year) DO UPDATE SET
        is_forecast = 0, source = 'edinet',
        {', '.join(f"{f} = CASE WHEN is_forecast THEN excluded.{f}"
                   f" ELSE COALESCE(excluded.{f}, {f}) END"
                   for f in EDINET_FIN_FIELDS)}
"""


class ApiError(Exception):
    """EDINET APIのエラー応答。認証エラー等はHTTP 200のJSONで返るので明示検知する。"""


def _get(session: requests.Session, url: str, params: Dict, sleep: float,
         timeout: int = 60) -> Optional[requests.Response]:
    """5xx/接続エラーは2回リトライ。404はNone。"""
    last_err = None
    for attempt in range(3):
        time.sleep(sleep if attempt == 0 else sleep * (attempt + 1) * 5)
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500 or resp.status_code == 429:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"fetch failed after retries: {url} ({last_err})")


def list_documents(session, day: date, api_key: str, sleep: float) -> List[Dict]:
    """書類一覧API。エラーはHTTP 200のJSONで返るのでmetadata.statusを見る。"""
    resp = _get(session, f"{API_BASE}/documents.json",
                {"date": day.isoformat(), "type": "2", "Subscription-Key": api_key},
                sleep, timeout=30)
    if resp is None:
        return []
    data = resp.json()
    # APIキー無効などはトップレベルのStatusCodeで返る(仕様書3-3)。
    # 空扱いにすると「新着なし」と区別がつかず静かに失敗するので中断する
    top = data.get("StatusCode")
    if top is not None and str(top) != "200":
        raise ApiError(f"書類一覧 {day}: {top} {data.get('message', '')}")
    status = str(data.get("metadata", {}).get("status", ""))
    if status != "200":
        if status not in ("404",):  # 404=保存期間外の日付など。それ以外は警告
            print(f"  [warn] 書類一覧 {day}: status={status} "
                  f"{data.get('metadata', {}).get('message', '')}", flush=True)
        return []
    return data.get("results", [])


def fetch_doc_csv(session, doc_id: str, api_key: str, sleep: float) -> Optional[bytes]:
    """書類取得API(type=5)。zip以外(エラーJSON)が返ったらNone。"""
    resp = _get(session, f"{API_BASE}/documents/{doc_id}",
                {"type": "5", "Subscription-Key": api_key}, sleep)
    if resp is None:
        return None
    content = resp.content
    if content[:1] == b"{":  # エラー時はJSONが返る
        try:
            data = json.loads(content)
        except ValueError:
            return None
        code = str(data.get("StatusCode", ""))
        if code == "404":
            return None
        raise ApiError(f"{doc_id}: {code} {data.get('message', '')}")
    if not content.startswith(b"PK"):  # zipマジック
        return None
    return content


def save_document(conn, code: str, doc: Dict, zip_bytes: bytes) -> tuple:
    """1書類分をパースしてDBへ。返り値は(財務行数, 大株主行数)。"""
    period_end = doc["periodEnd"]
    rows = read_jpcrp_rows(zip_bytes)
    fin_rows = parse_financials(rows, period_end)
    holder_rows = parse_holders(rows, period_end)
    business = parse_business(rows)

    raw_dir = DATA_DIR / "raw_edinet" / code
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{doc['docID']}.zip").write_bytes(zip_bytes)

    with conn:
        if fin_rows:
            # IR BANK由来の行とは(code, 年度)単位でマージする(単位はどちらも円)。
            # 重なる年度はEDINETの値が勝ち、EDINETに無いフィールド(設備投資等)や
            # 5年窓の外の古い年度はIR BANKの値が残る
            for row in fin_rows:
                values = [row.get(f) for f in EDINET_FIN_FIELDS]
                conn.execute(_UPSERT_SQL, [code, row["fiscal_year"]] + values)
        if holder_rows:
            # 同一時点のスナップショットは入れ替える(退出した株主が残らないように)
            conn.execute("DELETE FROM holders WHERE code = ? AND as_of = ?",
                         (code, holder_rows[0]["as_of"]))
        for h in holder_rows:
            conn.execute(
                """INSERT OR REPLACE INTO holders(code, as_of, rank, holder_name, ratio)
                   VALUES (?, ?, ?, ?, ?)""",
                (code, h["as_of"], h["rank"], h["holder_name"], h["ratio"]))
        if business:
            # 期末の古い有報(遅延提出・再取り込み)で新しい記述を上書きしない
            conn.execute(
                """INSERT INTO business(code, description, period_end, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     description=excluded.description,
                     period_end=excluded.period_end,
                     updated_at=excluded.updated_at
                   WHERE excluded.period_end >= period_end""",
                (code, business, period_end,
                 datetime.now().isoformat(timespec="seconds")))
        status = "ok" if (fin_rows or holder_rows) else "no_data"
        conn.execute(
            """INSERT OR REPLACE INTO edinet_docs
               (doc_id, code, doc_type_code, period_end, submitted_at, fetched_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (doc["docID"], code, doc["docTypeCode"], period_end,
             doc.get("submitDateTime"),
             datetime.now().isoformat(timespec="seconds"), status))
    return len(fin_rows), len(holder_rows)


def collect_filings(conn, session, api_key: str, days: int, sleep: float,
                    wanted_codes) -> List[Dict]:
    """直近days日の書類一覧を走査し、未取り込みの有報を提出日順で返す。"""
    companies = {r["code"] for r in conn.execute("SELECT code FROM companies")}
    known = {r["doc_id"] for r in conn.execute(
        "SELECT doc_id FROM edinet_docs WHERE status IN ('ok', 'no_data')")}

    filings = []
    today = date.today()
    for i in range(days - 1, -1, -1):  # 古い日付から
        day = today - timedelta(days=i)
        for doc in list_documents(session, day, api_key, sleep):
            if doc.get("docTypeCode") != DOC_TYPE_ANNUAL:
                continue
            if doc.get("csvFlag") != "1" or not doc.get("periodEnd"):
                continue
            if doc.get("withdrawalStatus") not in (None, "", "0"):
                continue
            sec = (doc.get("secCode") or "").strip()
            code = sec[:4] if len(sec) == 5 else None
            if code is None or code not in companies:
                continue
            if wanted_codes and code not in wanted_codes:
                continue
            if doc["docID"] in known:
                continue
            doc["_code"] = code
            filings.append(doc)
    filings.sort(key=lambda d: d.get("submitDateTime") or "")
    return filings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30,
                    help="今日から遡って書類一覧を走査する日数(初回は400程度)")
    ap.add_argument("--codes", help="カンマ区切りの証券コード(絞り込み)")
    ap.add_argument("--limit", type=int, help="最大取得書類数")
    ap.add_argument("--sleep", type=float, default=1.0, help="リクエスト間隔秒(既定1)")
    args = ap.parse_args()

    api_key = os.environ.get("EDINET_API_KEY")
    if not api_key:
        sys.exit("EDINET_API_KEY が未設定です。"
                 "EDINETトップページ https://disclosure2.edinet-fsa.go.jp/ の"
                 "「EDINET API」からユーザー登録(無料)してAPIキーを発行し、"
                 "export EDINET_API_KEY=... を設定してください")

    conn = get_conn()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    wanted = set(c.strip() for c in args.codes.split(",")) if args.codes else None

    print(f"書類一覧を{args.days}日分走査します(間隔{args.sleep}s)", flush=True)
    try:
        filings = collect_filings(conn, session, api_key, args.days, args.sleep, wanted)
    except ApiError as e:
        sys.exit(f"EDINET APIエラー: {e}")
    if args.limit:
        filings = filings[:args.limit]
    if not filings:
        print("新しい有報はありません")
        return
    print(f"{len(filings)}書類を取得します(推定{len(filings) * args.sleep / 60:.0f}分+)",
          flush=True)

    ok = no_data = errors = 0
    for i, doc in enumerate(filings, 1):
        code, doc_id = doc["_code"], doc["docID"]
        try:
            zip_bytes = fetch_doc_csv(session, doc_id, api_key, args.sleep)
            if zip_bytes is None:
                raise RuntimeError("CSVが取得できない(エラーJSONまたは404)")
            fin_count, holder_count = save_document(conn, code, doc, zip_bytes)
            if fin_count or holder_count:
                ok += 1
            else:
                no_data += 1
        except ApiError as e:
            # 認証エラー等は全書類で失敗するので続行しない
            sys.exit(f"EDINET APIエラー(中断。再実行で続きから): {e}")
        except Exception as e:
            errors += 1
            with conn:
                conn.execute(
                    """INSERT OR REPLACE INTO edinet_docs
                       (doc_id, code, doc_type_code, period_end, submitted_at,
                        fetched_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (doc_id, code, doc.get("docTypeCode", ""), doc.get("periodEnd"),
                     doc.get("submitDateTime"),
                     datetime.now().isoformat(timespec="seconds"), f"error:{e}"))
        if i % 50 == 0 or i == len(filings):
            print(f"  [{datetime.now():%H:%M}] {i}/{len(filings)} 完了 "
                  f"(ok={ok}, no_data={no_data}, error={errors})", flush=True)

    print(f"取得完了: ok={ok}, no_data={no_data}, error={errors}")


if __name__ == "__main__":
    main()
