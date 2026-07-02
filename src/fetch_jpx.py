"""JPX公表の上場銘柄一覧(data_j.xls)を取得してcompaniesテーブルを更新する。

使い方: .venv/bin/python -m src.fetch_jpx
"""
from datetime import datetime

import requests
import xlrd

from .db import DATA_DIR, get_conn

JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
TARGET_MARKETS = (
    "プライム（内国株式）",
    "スタンダード（内国株式）",
    "グロース（内国株式）",
)
USER_AGENT = "kabu-screener-personal/0.1 (personal use; contact: porukubodesu@gmail.com)"


def _cell_to_code(value) -> str:
    # コードは数値(1301.0)か英字入り文字列('130A')のどちらか
    if isinstance(value, float):
        return str(int(value))
    return str(value).strip()


def main():
    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    xls_path = raw_dir / "data_j.xls"

    resp = requests.get(JPX_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    xls_path.write_bytes(resp.content)

    wb = xlrd.open_workbook(str(xls_path))
    sh = wb.sheet_by_index(0)
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    seen = set()
    with conn:
        for r in range(1, sh.nrows):
            market = str(sh.cell_value(r, 3)).strip()
            if market not in TARGET_MARKETS:
                continue
            code = _cell_to_code(sh.cell_value(r, 1))
            name = str(sh.cell_value(r, 2)).strip()
            sector33 = str(sh.cell_value(r, 5)).strip()
            scale = str(sh.cell_value(r, 9)).strip()
            conn.execute(
                """INSERT INTO companies(code, name, market, sector33, scale, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, market=excluded.market,
                     sector33=excluded.sector33, scale=excluded.scale,
                     updated_at=excluded.updated_at""",
                (code, name, market.split("（")[0], sector33, scale, now),
            )
            seen.add(code)

        # 上場廃止・市場外移動した銘柄を除去(壊れたファイルで全消ししないようガード)
        removed = 0
        if len(seen) > 3000:
            placeholders = ",".join("?" * len(seen))
            removed = conn.execute(
                f"DELETE FROM companies WHERE code NOT IN ({placeholders})",
                list(seen),
            ).rowcount
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"上場銘柄マスタ更新: {len(seen)}件upsert / 廃止等の削除{removed}件 / 合計{total}社")


if __name__ == "__main__":
    main()
