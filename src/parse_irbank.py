"""IR BANKのCSV・HTMLをパースする純粋関数群(I/Oなし、テスト対象)。"""
import csv
import io
import re
from typing import Dict, List, Optional

from lxml import html as lxml_html

# fy-data-all.csv の列名 → financialsテーブルの列名
COLUMN_MAP = {
    "売上高": "revenue",
    "営業利益": "op_income",
    "経常利益": "ordinary_income",
    "純利益": "net_income",
    "EPS": "eps",
    "ROE": "roe",
    "ROA": "roa",
    "総資産": "total_assets",
    "純資産": "net_assets",
    "株主資本": "shareholders_equity",
    "利益剰余金": "retained_earnings",
    "BPS": "bps",
    "自己資本比率": "equity_ratio",
    "営業CF": "op_cf",
    "投資CF": "inv_cf",
    "財務CF": "fin_cf",
    "設備投資": "capex",
    "現金同等物": "cash",
    "営業CFマージン": "op_cf_margin",
    "一株配当": "dividend",
    "自社株買い": "buyback",
    "配当性向": "payout_ratio",
    "総還元性向": "total_return_ratio",
}

INT_FIELDS = {
    "revenue", "op_income", "ordinary_income", "net_income",
    "total_assets", "net_assets", "shareholders_equity", "retained_earnings",
    "op_cf", "inv_cf", "fin_cf", "capex", "cash", "buyback",
}

FISCAL_YEAR_RE = re.compile(r"^\d{4}/\d{2}$")
HOLDER_CELL_RE = re.compile(r"(\d+)\s*位\s*([\d.]+)%")


def _to_number(s: str, as_int: bool) -> Optional[float]:
    s = s.strip().replace(",", "")
    if s in ("", "-", "−", "ー"):
        return None
    try:
        return int(s) if as_int else float(s)
    except ValueError:
        return None


def parse_fy_csv(text: str) -> List[Dict]:
    """fy-data-all.csv をパースして年度ごとのdictリストを返す。

    フォーマット: タイトル行のあと「業績」「財務」「CF」「配当」の
    セクションが続き、各セクションは「年度,...」ヘッダ+データ行。
    予想行は末尾に「（予想）」列が付く。
    """
    rows: Dict[str, Dict] = {}
    columns: List[str] = []
    for record in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if not record or not record[0].strip():
            continue
        first = record[0].strip()
        if first == "年度":
            columns = [c.strip() for c in record]
            continue
        if not FISCAL_YEAR_RE.match(first) or not columns:
            continue  # タイトル行・セクション名行
        fy = first
        is_forecast = any("予想" in c for c in record[len(columns):]) or any(
            "予想" in c for c in record
        )
        row = rows.setdefault(fy, {"fiscal_year": fy, "is_forecast": 0})
        if is_forecast:
            row["is_forecast"] = 1
        for col_name, value in zip(columns[1:], record[1:]):
            field = COLUMN_MAP.get(col_name)
            if field is None:
                continue
            num = _to_number(value, as_int=field in INT_FIELDS)
            if num is not None:
                row[field] = num
    return [rows[k] for k in sorted(rows)]


def parse_holder_html(text: str) -> List[Dict]:
    """大株主ページ(/{code}/holder)の「保有推移」テーブルをパース。

    返り値: [{"as_of": "2026/03", "rank": 1, "holder_name": ..., "ratio": 12.8}, ...]
    全時点分を返す(著名人の登場履歴やVC退出の確認に使うため)。
    """
    doc = lxml_html.fromstring(text)
    tables = doc.xpath("//table")
    if not tables:
        return []
    table = tables[0]
    rows = table.xpath(".//tr")
    if len(rows) < 2:
        return []
    header = [c.text_content().strip() for c in rows[0].xpath("./th|./td")]
    dates = [h for h in header[1:] if FISCAL_YEAR_RE.match(h)]
    out: List[Dict] = []
    for tr in rows[1:]:
        cells = tr.xpath("./th|./td")
        if len(cells) < 2:
            continue
        name = cells[0].text_content().strip()
        if not name:
            continue
        for as_of, cell in zip(dates, cells[1:]):
            m = HOLDER_CELL_RE.search(cell.text_content())
            if m:
                out.append({
                    "as_of": as_of,
                    "rank": int(m.group(1)),
                    "holder_name": name,
                    "ratio": float(m.group(2)),
                })
    return out
