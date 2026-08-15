"""EDINET API v2 のCSV(XBRL_TO_CSV)をパースする純粋関数群(I/Oなし、テスト対象)。

EDINETの書類取得API(type=5)は zip を返し、中の XBRL_TO_CSV/jpcrp*.csv に
書類の全インスタンス値が入っている(タブ区切り・UTF-16LE・全値ダブルクォート)。
列: 要素ID / 項目名 / コンテキストID / 相対年度 / 連結・個別 / 期間・時点 / ユニットID / 単位 / 値

方針:
- 「主要な経営指標等の推移」(要素IDに SummaryOfBusinessResults を含む)から
  直近5年分を取る。営業利益はサマリーに無いことが多いので財務諸表本体
  (jppfs_cor:OperatingIncome 等)からも拾う(当期+前期の2年分)。
- 要素IDの網羅列挙は会計基準・業種で発散するため、項目名(日本語ラベル)の
  キーワードで対応付ける(IR BANKパーサと同じ発想)。ラベルはNFKC正規化する。
  キーワードは前にあるものほど優先(IFRSの「親会社の所有者に帰属する当期利益」を
  非支配持分込みの「当期利益」より優先する等)。
- 連結を優先する。個別(単体)での補完は、連結データが無い会社(非連結)と
  提出会社のみの指標(1株配当・配当性向・株式数)に限る。連結会社の単体値
  (例: IFRSで連結営業利益が無い場合の単体営業利益)を混ぜると連結売上と
  スケールが合わないため捨てる。
- 金額は円のまま(IR BANK由来のデータと同じ単位)。比率はXBRLでは純小数
  (0.585 = 58.5%)なので100倍して%に揃える。
"""
import csv
import html
import io
import re
import unicodedata
import zipfile
from typing import Dict, List, Optional

# コンテキストID: 当期/N期前 × 期間/時点 × (連結 | 個別=NonConsolidatedMember)
# これ以外(セグメント情報などのMember付き)は対象外
CTX_RE = re.compile(
    r"^(Current|Prior(\d+))Year(Duration|Instant)(_NonConsolidatedMember)?$")
HOLDER_CTX_RE = re.compile(r"^CurrentYearInstant_No(\d+)MajorShareholdersMember$")

# field -> (含むキーワード[前ほど優先], 除外キーワード, スコープ)
# スコープ: 'summary'=経営指標サマリーのみ / 'any'=財務諸表本体も可
FIELD_SPECS = {
    "revenue": (("売上高", "売上収益", "営業収益", "経常収益", "営業収入",
                 "営業総収入", "事業収益", "完成工事高"),
                ("率", "1株"), "summary"),
    "op_income": (("営業利益",), ("率", "セグメント"), "any"),
    "ordinary_income": (("経常利益",), ("率",), "summary"),
    "net_income": (("親会社株主に帰属する当期純利益", "親会社の所有者に帰属する当期利益",
                    "当期純利益", "当期利益"),
                   ("1株", "潜在", "率", "非支配"), "summary"),
    "eps": (("1株当たり当期純利益", "基本的1株当たり当期利益"), ("潜在",), "summary"),
    "roe": (("自己資本利益率", "親会社所有者帰属持分利益率"), (), "summary"),
    "total_assets": (("総資産額",), (), "summary"),
    "net_assets": (("純資産額", "親会社の所有者に帰属する持分"),
                   ("1株", "率"), "summary"),
    "bps": (("1株当たり純資産", "1株当たり親会社所有者帰属持分"), (), "summary"),
    "equity_ratio": (("自己資本比率", "親会社所有者帰属持分比率",
                      "親会社の所有者に帰属する持分比率"), (), "summary"),
    "op_cf": (("営業活動によるキャッシュ",), (), "summary"),
    "inv_cf": (("投資活動によるキャッシュ",), (), "summary"),
    "fin_cf": (("財務活動によるキャッシュ",), (), "summary"),
    "cash": (("現金及び現金同等物",), (), "summary"),
    "dividend": (("1株当たり配当額",), ("中間",), "summary"),
    "payout_ratio": (("配当性向",), (), "summary"),
    "shares_issued": (("発行済株式総数",), (), "summary"),
}

INT_FIELDS = {
    "revenue", "op_income", "ordinary_income", "net_income",
    "total_assets", "net_assets", "op_cf", "inv_cf", "fin_cf", "cash",
    "shares_issued",
}
# 要素IDベースの対応(ラベルより優先)。親会社帰属の利益・持分はラベルの語順が
# 発行者ごとに発散する(「当期利益又は当期損失(△):親会社の所有者に帰属」等)ため、
# タクソノミで安定している要素IDの部分一致で拾う。field -> (含む, 除外)
ELEMENT_SPECS = {
    "net_income": (("ProfitLossAttributableToOwnersOfParent",), ()),
    "net_assets": (("EquityAttributableToOwnersOfParent",), ("PerShare",)),
    "bps": (("EquityToEquityAttributableToOwnersOfParentPerShare",), ()),
}

# XBRLの純小数(0.585)を%表記(58.5)に揃えるフィールド
# TODO: 実データで表記(純小数かどうか)を検証する(APIキー取得後)
RATIO_FIELDS = {"roe", "equity_ratio", "payout_ratio"}
# 提出会社(個別)の値でも常に採用してよいフィールド(会社単位の指標)。
# これ以外は、連結会社では個別値を捨てる(連結売上とスケールが合わないため)
PARENT_ONLY_FIELDS = {"dividend", "payout_ratio", "shares_issued"}
# 「連結会社かどうか」の判定に使うフィールド(連結値が1つでもあれば連結会社)
_SCALE_TRIO = ("revenue", "net_income", "total_assets")


def _to_number(s: str, as_int: bool) -> Optional[float]:
    s = (s or "").strip().replace(",", "")
    if s in ("", "-", "－", "−", "ー", "―"):
        return None
    try:
        return int(float(s)) if as_int else float(s)
    except ValueError:
        return None


def read_jpcrp_rows(zip_bytes: bytes) -> List[Dict]:
    """type=5 のzipから本文書(jpcrp*.csv)を読み、行dictのリストを返す。

    監査報告書(jpaud*.csv)は対象外。項目名はNFKC正規化して返す
    (全角の「１株当たり」「（△）」等を半角に揃えるため)。
    """
    out: List[Dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist()
                 if "XBRL_TO_CSV/" in n and n.endswith(".csv")
                 and "jpcrp" in n.rsplit("/", 1)[-1]]
        for name in names:
            text = zf.read(name).decode("utf-16-le", errors="replace").lstrip("﻿")
            reader = csv.reader(io.StringIO(text), delimiter="\t")
            header = next(reader, None)
            if not header:
                continue
            idx = {col.strip(): i for i, col in enumerate(header)}
            required = ("要素ID", "コンテキストID", "値")
            if any(col not in idx for col in required):
                continue
            for rec in reader:
                if len(rec) <= idx["値"]:
                    continue
                out.append({
                    "element": rec[idx["要素ID"]].strip(),
                    "label": unicodedata.normalize(
                        "NFKC", rec[idx.get("項目名", idx["要素ID"])].strip()),
                    "context": rec[idx["コンテキストID"]].strip(),
                    "unit": rec[idx["単位"]].strip() if "単位" in idx and len(rec) > idx["単位"] else "",
                    "value": rec[idx["値"]].strip(),
                })
    return out


def _fiscal_year(period_end: str, offset: int) -> str:
    """'2026-03-31' と N期前オフセットから '2026/03' 形式を作る。"""
    y, m = int(period_end[:4]), period_end[5:7]
    return f"{y - offset}/{m}"


def parse_financials(rows: List[Dict], period_end: str) -> List[Dict]:
    """経営指標サマリー+財務諸表本体から年度ごとのdictリストを返す。

    返り値はparse_irbank.parse_fy_csvと同じ形(値が取れたキーだけ持つ)。
    is_forecastは常に0(有報は実績のみ)。
    """
    # (offset, field) -> (値, 連結かどうか, キーワード優先度)。
    # 連結を個別より優先し、同格ならキーワード優先度(FIELD_SPECSの並び順)で選ぶ
    acc: Dict[tuple, tuple] = {}
    for row in rows:
        m = CTX_RE.match(row["context"])
        if not m:
            continue
        offset = int(m.group(2)) if m.group(2) else 0
        consolidated = m.group(4) is None
        is_summary = "SummaryOfBusinessResults" in row["element"]
        label = row["label"]
        # 1) 要素IDでの対応(サマリーのみ)。当たればキーワードより優先(rank=-1)
        field = rank = None
        if is_summary:
            for f, (marks, ex_marks) in ELEMENT_SPECS.items():
                if (any(k in row["element"] for k in marks)
                        and not any(k in row["element"] for k in ex_marks)):
                    field, rank = f, -1
                    break
        # 2) ラベルキーワードでの対応
        if field is None:
            for f, (inc, exc, scope) in FIELD_SPECS.items():
                if scope == "summary" and not is_summary:
                    continue
                r = next((i for i, k in enumerate(inc) if k in label), None)
                if r is None or any(k in label for k in exc):
                    continue
                field, rank = f, r
                break  # 1行が複数フィールドに一致することはない前提
        if field is None:
            continue
        num = _to_number(row["value"], as_int=field in INT_FIELDS)
        if num is None:
            continue
        if field in RATIO_FIELDS:
            num = round(num * 100, 2)
        key = (offset, field)
        cur = acc.get(key)
        if cur is None or (consolidated, -rank) > (cur[1], -cur[2]):
            acc[key] = (num, consolidated, rank)

    # 連結値が1つでもあれば連結会社とみなし、個別値は会社単位の指標以外捨てる
    has_consolidated = any(
        c for (_, f), (_, c, _) in acc.items() if f in _SCALE_TRIO)

    offsets = sorted({off for off, _ in acc},
                     reverse=True)  # 古い年度から
    out: List[Dict] = []
    for off in offsets:
        fields = {}
        for (o, f), (v, c, _) in acc.items():
            if o != off:
                continue
            if not c and has_consolidated and f not in PARENT_ONLY_FIELDS:
                continue
            fields[f] = v
        # 売上も利益も総資産も無いオフセットはノイズとみなして捨てる
        if not any(f in fields for f in _SCALE_TRIO):
            continue
        row = {"fiscal_year": _fiscal_year(period_end, off), "is_forecast": 0}
        row.update(fields)
        if fields.get("op_cf") is not None and fields.get("revenue"):
            row["op_cf_margin"] = round(fields["op_cf"] / fields["revenue"] * 100, 2)
        out.append(row)
    return out


# 「事業の内容」(第一部【企業情報】)のTextBlock。値はHTML
BUSINESS_ELEMENT = "DescriptionOfBusinessTextBlock"

_BLOCK_TAG_RE = re.compile(
    r"</(?:p|div|tr|li|h[1-6]|table)>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """TextBlockのHTMLをプレーンテキストに。ブロック要素の境界は改行として残す。"""
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    lines = (" ".join(ln.split()) for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


def parse_business(rows: List[Dict]) -> Optional[str]:
    """「事業の内容」をプレーンテキストで返す。無ければNone。"""
    for row in rows:
        if row["element"].rsplit(":", 1)[-1] == BUSINESS_ELEMENT and row["value"]:
            text = strip_html(row["value"])
            if text:
                return text
    return None


def parse_holders(rows: List[Dict], period_end: str) -> List[Dict]:
    """「大株主の状況」(当期末の上位10名)をパースする。

    返り値はparse_irbank.parse_holder_htmlと同じ形。as_ofは期末('2026/03')。
    保有割合はXBRLでは純小数なので%に揃える。
    """
    names: Dict[int, str] = {}
    ratios: Dict[int, float] = {}
    for row in rows:
        m = HOLDER_CTX_RE.match(row["context"])
        if not m:
            continue
        no = int(m.group(1))
        local = row["element"].rsplit(":", 1)[-1]
        if local == "NameMajorShareholders":
            # 全角英字のままだとclassify_holderの法人マーカーに当たらないので
            # NFKC正規化して保存する(ＪＰモルガン→JPモルガン 等)
            name = " ".join(unicodedata.normalize("NFKC", row["value"]).split())
            if name:
                names[no] = name
        elif local == "ShareholdingRatio":
            num = _to_number(row["value"], as_int=False)
            if num is not None:
                ratios[no] = round(num * 100, 2)

    as_of = _fiscal_year(period_end, 0)
    return [
        {"as_of": as_of, "rank": no, "holder_name": names[no],
         "ratio": ratios.get(no)}
        for no in sorted(names)
    ]
