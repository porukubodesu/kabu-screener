"""EDINET API(v2)から有価証券報告書を取得してDBに格納する。

金融庁の公式APIなのでIR BANKのような表示制限はないが、常識的なペースで
アクセスする(既定0.5秒間隔)。APIキーは無料登録して環境変数で渡す:
  export EDINET_API_KEY=...   # https://api.edinet-fsa.go.jp (EDINET API機能)

2段階で動く:
1. 書類一覧APIを日付ごとに走査して有報(docTypeCode=120)の目録を作る
   (edinet_docsテーブル。走査済みの日付はedinet_index_logでスキップ)
2. 各社の最新の有報CSV(type=5)をダウンロードして financials / holders に取り込む
   - 主要な経営指標等の推移 → 5期分の財務(IR BANKの4〜5期より1期多い)
   - 大株主の状況 → 当期末の上位10名
   - 生ZIPは data/raw_edinet/ に保存(パーサ修正時は再ダウンロード不要)

使い方:
  .venv/bin/python -m src.fetch_edinet --index-only        # 目録づくりだけ
  .venv/bin/python -m src.fetch_edinet --limit 50          # 目録+50社取り込み
  .venv/bin/python -m src.fetch_edinet                     # 目録+未取り込みぜんぶ
  .venv/bin/python -m src.fetch_edinet --codes 7203,9983   # 指定銘柄のみ
"""
import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from .db import DATA_DIR, get_conn
from .fetch_irbank import FIN_FIELDS
from .parse_edinet import parse_document

LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
USER_AGENT = "kabu-screener-personal/0.1 (personal use; contact: porukubodesu@gmail.com)"
DOC_TYPE_YUHO = "120"  # 有価証券報告書

EDINET_FIN_FIELDS = FIN_FIELDS + ["shares_issued"]


class ApiKeyError(Exception):
    """APIキーが未設定・無効。"""


def _api_key() -> str:
    key = os.environ.get("EDINET_API_KEY", "").strip()
    if not key:
        raise ApiKeyError(
            "環境変数 EDINET_API_KEY が未設定です。"
            "https://api.edinet-fsa.go.jp でAPIキーを無料登録してください")
    return key


def _get(session: requests.Session, url: str, params: dict, sleep: float) -> requests.Response:
    """5xx/接続エラーは2回リトライ。401/403はAPIキーエラーとして即中断。"""
    last_err = None
    for attempt in range(3):
        time.sleep(sleep if attempt == 0 else sleep * (attempt + 1) * 4)
        try:
            resp = session.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            last_err = e
            continue
        if resp.status_code in (401, 403):
            raise ApiKeyError(f"EDINET APIがHTTP {resp.status_code}を返しました。"
                              "EDINET_API_KEYを確認してください")
        if resp.status_code >= 500:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"fetch failed after retries: {url} ({last_err})")


def _sec_to_code(sec_code: str) -> Optional[str]:
    """EDINETのsecCode(5桁 '72030')を証券コード4桁('7203')にする。"""
    s = (sec_code or "").strip()
    if len(s) == 5 and s.endswith("0"):
        return s[:4]
    if len(s) == 4:
        return s
    return None


def index_documents(conn, session, days: int, sleep: float):
    """書類一覧APIを走査して有報の目録(edinet_docs)を更新する。"""
    key = _api_key()
    today = date.today()
    dates = [today - timedelta(days=n) for n in range(days)]
    done = {r["list_date"] for r in conn.execute("SELECT list_date FROM edinet_index_log")}
    # 当日分は提出が増えるので毎回見直す
    todo = [d for d in dates if d.isoformat() not in done or d == today]
    if not todo:
        return
    print(f"書類一覧を走査: {len(todo)}日分(過去{days}日のうち未走査分)", flush=True)

    found = 0
    for n, d in enumerate(sorted(todo), 1):
        resp = _get(session, LIST_URL,
                    {"date": d.isoformat(), "type": "2", "Subscription-Key": key}, sleep)
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            payload = {}
        results = payload.get("results") or []
        count = 0
        with conn:
            for doc in results:
                if doc.get("docTypeCode") != DOC_TYPE_YUHO:
                    continue
                if doc.get("withdrawalStatus") not in (None, "", "0"):
                    continue
                if doc.get("csvFlag") not in ("1", 1):
                    continue
                code = _sec_to_code(doc.get("secCode"))
                if not code or not doc.get("docID"):
                    continue
                conn.execute(
                    """INSERT INTO edinet_docs
                         (doc_id, code, edinet_code, filer_name, doc_type,
                          period_end, submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(doc_id) DO NOTHING""",
                    (doc["docID"], code, doc.get("edinetCode"), doc.get("filerName"),
                     doc["docTypeCode"], doc.get("periodEnd"), doc.get("submitDateTime")),
                )
                count += 1
            conn.execute(
                """INSERT OR REPLACE INTO edinet_index_log(list_date, indexed_at, doc_count)
                   VALUES (?, ?, ?)""",
                (d.isoformat(), datetime.now().isoformat(timespec="seconds"), count),
            )
        found += count
        if n % 50 == 0 or n == len(todo):
            print(f"  [{datetime.now():%H:%M}] {n}/{len(todo)}日 走査(有報{found}件)", flush=True)


def pick_targets(conn, codes: Optional[str], limit: Optional[int]):
    """各社の最新の有報のうち、未取り込みのものを返す。"""
    sql = """SELECT d.* FROM edinet_docs d
             JOIN companies c ON c.code = d.code
             WHERE d.submitted_at = (SELECT MAX(submitted_at) FROM edinet_docs d2
                                     WHERE d2.code = d.code)
               AND d.ingested_at IS NULL
             ORDER BY d.code"""
    rows = [dict(r) for r in conn.execute(sql)]
    if codes:
        wanted = {c.strip() for c in codes.split(",") if c.strip()}
        rows = [r for r in rows if r["code"] in wanted]
    return rows[:limit] if limit else rows


def save_document(conn, code: str, doc_id: str, fin_rows, holder_rows):
    fin_count = holder_count = 0
    with conn:
        if fin_rows:
            # 年度の入れ替え(EDINETの5期分で全面的に置き換える)
            conn.execute("DELETE FROM financials WHERE code = ?", (code,))
            for row in fin_rows:
                values = [row.get(f) for f in EDINET_FIN_FIELDS]
                conn.execute(
                    f"""INSERT OR REPLACE INTO financials
                        (code, fiscal_year, is_forecast, {', '.join(EDINET_FIN_FIELDS)})
                        VALUES (?, ?, ?, {', '.join('?' * len(EDINET_FIN_FIELDS))})""",
                    [code, row["fiscal_year"], row["is_forecast"]] + values,
                )
            fin_count = len(fin_rows)
        if holder_rows:
            # 同一時点のスナップショットのみ入れ替え(IR BANK由来の過去履歴は残す)
            as_ofs = {h["as_of"] for h in holder_rows}
            for as_of in as_ofs:
                conn.execute("DELETE FROM holders WHERE code = ? AND as_of = ?",
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
        conn.execute("UPDATE edinet_docs SET ingested_at = ? WHERE doc_id = ?",
                     (datetime.now().isoformat(timespec="seconds"), doc_id))
    return fin_count, holder_count


def fetch_documents(conn, session, targets, sleep: float):
    key = _api_key()
    raw_dir = DATA_DIR / "raw_edinet"
    raw_dir.mkdir(parents=True, exist_ok=True)

    est_h = len(targets) * (sleep + 1.0) / 3600
    print(f"{len(targets)}社の有報を取り込みます(間隔{sleep}s、推定{est_h:.1f}時間)", flush=True)

    ok = no_data = errors = 0
    for i, doc in enumerate(targets, 1):
        code, doc_id = doc["code"], doc["doc_id"]
        try:
            zip_path = raw_dir / f"{doc_id}.zip"
            if zip_path.exists():
                zip_bytes = zip_path.read_bytes()
            else:
                resp = _get(session, DOC_URL.format(doc_id=doc_id),
                            {"type": "5", "Subscription-Key": key}, sleep)
                zip_bytes = resp.content
                if not zip_bytes.startswith(b"PK"):
                    # ZIPでない=エラーJSONが返った
                    raise RuntimeError(f"not a zip: {zip_bytes[:100]!r}")
                zip_path.write_bytes(zip_bytes)
            fin_rows, holder_rows = parse_document(zip_bytes)
        except ApiKeyError:
            raise
        except Exception as e:
            errors += 1
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO fetch_log(code, fetched_at, status) VALUES (?, ?, ?)",
                    (code, datetime.now().isoformat(timespec="seconds"), f"error:{e}"),
                )
            continue

        fin_count, holder_count = save_document(conn, code, doc_id, fin_rows, holder_rows)
        if fin_count or holder_count:
            ok += 1
        else:
            no_data += 1
        if i % 50 == 0 or i == len(targets):
            print(f"  [{datetime.now():%H:%M}] {i}/{len(targets)} 完了 "
                  f"(ok={ok}, no_data={no_data}, error={errors})", flush=True)

    print(f"取り込み完了: ok={ok}, no_data={no_data}, error={errors}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", help="カンマ区切りの証券コード(このコードのみ取り込む)")
    ap.add_argument("--limit", type=int, help="最大取り込み社数")
    ap.add_argument("--index-days", type=int, default=400,
                    help="書類一覧を遡る日数(既定400。有報は年1回なので1年+余裕)")
    ap.add_argument("--index-only", action="store_true", help="目録づくりのみで終了")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="APIリクエストの間隔秒(既定0.5)")
    args = ap.parse_args()

    conn = get_conn()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    try:
        index_documents(conn, session, args.index_days, args.sleep)
        if args.index_only:
            n = conn.execute("SELECT COUNT(DISTINCT code) FROM edinet_docs").fetchone()[0]
            print(f"目録: {n}社分の有報を把握済み")
            return
        targets = pick_targets(conn, args.codes, args.limit)
        if not targets:
            print("対象なし(全銘柄取り込み済みか、目録に有報がありません)")
            return
        fetch_documents(conn, session, targets, args.sleep)
    except ApiKeyError as e:
        raise SystemExit(f"中断: {e}")


if __name__ == "__main__":
    main()
