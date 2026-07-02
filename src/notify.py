"""最新スクリーニング結果から未通知の上位1銘柄を「今日の1銘柄」として通知する。

LINE Messaging APIの環境変数があればpush、なければ標準出力に表示。
  LINE_CHANNEL_ACCESS_TOKEN: Messaging APIのチャネルアクセストークン
  LINE_USER_ID: push先のユーザーID(既存の秘書ボットと同じチャネルでOK)

使い方: .venv/bin/python -m src.notify        # cronで1日1回
        .venv/bin/python -m src.notify --dry-run
"""
import argparse
import json
import os
from datetime import datetime

import requests

from .db import get_conn

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def format_card(code: str, name: str, market: str, rank: int, score: float, m: dict) -> str:
    def pct(v, signed=False):
        if v is None:
            return "-"
        return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"

    lines = [
        f"📈 今日の1銘柄: {name} ({code}/{market})",
        f"スコア {score:.3f}(候補内{rank}位)",
        "",
        f"売上CAGR: {pct(m.get('rev_cagr'))}({m.get('years')}期分)",
        f"連続増収増益: {m.get('consec_growth')}期",
        f"営業CFマージン: {m.get('op_cf_margin', '-')}%(3期平均)",
        f"自己資本比率: {m.get('equity_ratio', '-')}%",
        f"株式数変化: {pct(m.get('dilution'), signed=True)}(希薄化チェック)",
        f"オーナー: {' / '.join(m.get('owner_names') or ['-'])}",
        "",
        f"四季報で確認 → https://irbank.net/{code}",
    ]
    return "\n".join(lines)


def push_line(text: str) -> bool:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        return False
    resp = requests.post(
        LINE_PUSH_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=30,
    )
    resp.raise_for_status()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="通知済みフラグを立てない")
    args = ap.parse_args()

    conn = get_conn()
    latest = conn.execute("SELECT MAX(run_date) FROM screen_results").fetchone()[0]
    if not latest:
        print("スクリーニング結果がありません。先に python -m src.screen を実行してください")
        return
    row = conn.execute(
        """SELECT * FROM screen_results
           WHERE run_date = ? AND notified_at IS NULL
           ORDER BY rank LIMIT 1""",
        (latest,),
    ).fetchone()
    if not row:
        print(f"{latest} の候補はすべて通知済みです")
        return

    comp = conn.execute(
        "SELECT name, market FROM companies WHERE code = ?", (row["code"],)
    ).fetchone()
    name, market = (comp["name"], comp["market"]) if comp else (row["code"], "?")
    card = format_card(row["code"], name, market, row["rank"], row["score"],
                       json.loads(row["metrics_json"]))

    sent = push_line(card)
    print(card)
    print(f"\n[{'LINEに送信しました' if sent else 'LINE未設定のため表示のみ(環境変数を設定してください)'}]")

    if not args.dry_run:
        with conn:
            conn.execute(
                "UPDATE screen_results SET notified_at = ? WHERE run_date = ? AND code = ?",
                (datetime.now().isoformat(timespec="seconds"), latest, row["code"]),
            )


if __name__ == "__main__":
    main()
