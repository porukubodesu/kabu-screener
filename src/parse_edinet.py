"""EDINET APIのCSV書類(type=5)をパースする純粋関数群(I/Oなし、テスト対象)。

有価証券報告書のCSV(XBRLをフラット化したもの)から:
- 「主要な経営指標等の推移」(5期分) → financials相当の年度別dict
- 「大株主の状況」(直近期末の上位10名) → holders相当のdict
を取り出す。

CSVの形式: UTF-16LE・タブ区切り。列は
  要素ID, 項目名, コンテキストID, 相対年度, 連結・個別, 期間・時点, ユニットID, 単位, 値
コンテキストIDの例: CurrentYearDuration, Prior1YearInstant,
CurrentYearInstant_No1MajorShareholdersMember,
CurrentYearDuration_NonConsolidatedMember(個別)。

会計基準・業種で要素IDが変わるため、各フィールドは候補リストの先頭から
値が存在するものを採用する。連結があれば連結を、なければ個別を使う。
"""
import csv
import io
import re
import zipfile
from typing import Dict, List, Optional, Tuple

# 相対年度: コンテキストIDの接頭辞 → 当期から何期前か
_YEAR_OFFSET = {
    "CurrentYear": 0,
    "Prior1Year": 1,
    "Prior2Year": 2,
    "Prior3Year": 3,
    "Prior4Year": 4,
}
_CONTEXT_RE = re.compile(
    r"^(CurrentYear|Prior[1-4]Year)(Duration|Instant)(?:_(\S+))?$")
_HOLDER_MEMBER_RE = re.compile(r"^No(\d+)MajorShareholdersMember$")

# financialsの列 → 要素ID候補(名前空間接頭辞は除いたローカル名、優先順)。
# 主要な経営指標(SummaryOfBusinessResults)は日本基準・IFRS・米国基準・
# 銀行等の業種でタグが分かれるので、代表的なものを並べる。
SUMMARY_ELEMENTS: Dict[str, List[str]] = {
    "revenue": [
        "NetSalesSummaryOfBusinessResults",
        "RevenueIFRSSummaryOfBusinessResults",
        "SalesIFRSSummaryOfBusinessResults",
        "RevenuesUSGAAPSummaryOfBusinessResults",
        "OperatingRevenue1SummaryOfBusinessResults",
        "OperatingRevenue2SummaryOfBusinessResults",
        "GrossOperatingRevenueSummaryOfBusinessResults",
        "ContractsCompletedRevOpRevenueSummaryOfBusinessResults",
        "OperatingIncomeRevenueSummaryOfBusinessResults",
        "OrdinaryIncomeSummaryOfBusinessResults",  # 銀行の経常収益
    ],
    "ordinary_income": [
        "OrdinaryIncomeLossSummaryOfBusinessResults",
        "ProfitBeforeTaxIFRSSummaryOfBusinessResults",   # IFRSは税引前利益で代用
        "IncomeBeforeIncomeTaxesUSGAAPSummaryOfBusinessResults",
    ],
    "net_income": [
        "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
        "NetIncomeLossSummaryOfBusinessResults",
        "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
        "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
    ],
    "total_assets": [
        "TotalAssetsSummaryOfBusinessResults",
        "TotalAssetsIFRSSummaryOfBusinessResults",
        "TotalAssetsUSGAAPSummaryOfBusinessResults",
    ],
    "net_assets": [
        "NetAssetsSummaryOfBusinessResults",
        "TotalEquityIFRSSummaryOfBusinessResults",
        "EquityIncludingPortionAttributableToNonControllingInterestUSGAAPSummaryOfBusinessResults",
    ],
    "bps": [
        "NetAssetsPerShareSummaryOfBusinessResults",
        "EquityAttributableToOwnersOfParentPerShareIFRSSummaryOfBusinessResults",
        "EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults",
    ],
    "eps": [
        "BasicEarningsLossPerShareSummaryOfBusinessResults",
        "BasicEarningsPerShareIFRSSummaryOfBusinessResults",
        "BasicEarningsPerShareUSGAAPSummaryOfBusinessResults",
    ],
    "equity_ratio": [
        "EquityToAssetRatioSummaryOfBusinessResults",
        "EquityToAssetRatioIFRSSummaryOfBusinessResults",
        "EquityToAssetRatioUSGAAPSummaryOfBusinessResults",
    ],
    "roe": [
        "RateOfReturnOnEquitySummaryOfBusinessResults",
        "RateOfReturnOnEquityIFRSSummaryOfBusinessResults",
        "RateOfReturnOnEquityUSGAAPSummaryOfBusinessResults",
    ],
    "op_cf": [
        "NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults",
        "CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults",
        "NetCashProvidedByUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults",
    ],
    "inv_cf": [
        "NetCashProvidedByUsedInInvestingActivitiesSummaryOfBusinessResults",
        "CashFlowsFromUsedInInvestingActivitiesIFRSSummaryOfBusinessResults",
        "NetCashProvidedByUsedInInvestingActivitiesUSGAAPSummaryOfBusinessResults",
    ],
    "fin_cf": [
        "NetCashProvidedByUsedInFinancingActivitiesSummaryOfBusinessResults",
        "CashFlowsFromUsedInFinancingActivitiesIFRSSummaryOfBusinessResults",
        "NetCashProvidedByUsedInFinancingActivitiesUSGAAPSummaryOfBusinessResults",
    ],
    "cash": [
        "CashAndCashEquivalentsSummaryOfBusinessResults",
        "CashAndCashEquivalentsIFRSSummaryOfBusinessResults",
        "CashAndCashEquivalentsUSGAAPSummaryOfBusinessResults",
    ],
    "dividend": [
        "DividendPaidPerShareSummaryOfBusinessResults",
    ],
    "payout_ratio": [
        "PayoutRatioSummaryOfBusinessResults",
        "PayoutRatioIFRSSummaryOfBusinessResults",
        "PayoutRatioUSGAAPSummaryOfBusinessResults",
    ],
    "shares_issued": [
        "TotalNumberOfIssuedSharesSummaryOfBusinessResults",
    ],
}

# 営業利益は主要な経営指標に含まれないため、財務諸表本体(当期・前期の2期分)から補完
OP_INCOME_ELEMENTS = [
    "OperatingIncome",            # jppfs_cor(日本基準PL)
    "OperatingProfitLossIFRS",    # jpigp_cor(IFRS)
]

# 比率系: XBRLでは小数(0.378=37.8%)で入ることが多いが、%表記の提出もある。
# 会社単位で |値|<=1 に収まっていれば小数とみなして100倍する。
RATIO_FIELDS = ("equity_ratio", "roe", "payout_ratio")

INT_FIELDS = {
    "revenue", "op_income", "ordinary_income", "net_income",
    "total_assets", "net_assets", "op_cf", "inv_cf", "fin_cf", "cash",
    "shares_issued",
}

HOLDER_NAME_ELEMENT = "NameMajorShareholders"
HOLDER_RATIO_ELEMENT = "ShareholdingRatio"
PERIOD_END_ELEMENT = "CurrentPeriodEndDateDEI"


def _to_number(s: str) -> Optional[float]:
    s = s.strip().replace(",", "")
    if s in ("", "-", "−", "ー", "―", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def decode_csv(data: bytes) -> List[Dict[str, str]]:
    """EDINETのCSVバイト列を行dictのリストにする(UTF-16LE/タブ区切り)。"""
    try:
        text = data.decode("utf-16")
    except UnicodeDecodeError:
        text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = next(reader, None)
    if not header:
        return []
    header = [h.strip('"').strip() for h in header]
    rows = []
    for rec in reader:
        if len(rec) < len(header):
            continue
        rows.append({h: v.strip('"').strip() for h, v in zip(header, rec)})
    return rows


def _local_name(element_id: str) -> str:
    return element_id.split(":", 1)[1] if ":" in element_id else element_id


def _parse_context(context_id: str) -> Optional[Tuple[int, str, Optional[str]]]:
    """コンテキストID → (何期前か, Duration/Instant, メンバー名) 。対象外はNone。"""
    m = _CONTEXT_RE.match(context_id)
    if not m:
        return None
    return _YEAR_OFFSET[m.group(1)], m.group(2), m.group(3)


def _fiscal_year_label(period_end: str, years_back: int) -> Optional[str]:
    """'2026-03-31' と n期前 → '2026/03' 形式(既存DBの表記に合わせる)。"""
    m = re.match(r"^(\d{4})-(\d{2})-\d{2}$", period_end or "")
    if not m:
        return None
    return f"{int(m.group(1)) - years_back}/{m.group(2)}"


def parse_financials(rows: List[Dict[str, str]]) -> List[Dict]:
    """主要な経営指標(5期分)+PLの営業利益(2期分)を年度別dictにする。

    返り値は fetch_irbank.save_company が扱うのと同じ形:
    [{"fiscal_year": "2022/03", "is_forecast": 0, "revenue": ..., ...}, ...]
    """
    period_end = next(
        (r["値"] for r in rows if _local_name(r.get("要素ID", "")) == PERIOD_END_ELEMENT),
        None)
    if not period_end:
        return []

    # (フィールド, 候補優先順位, 期, 連結か) → 値 を集める
    collected: Dict[Tuple[str, int, int, bool], float] = {}
    element_to_field = {}
    for field, candidates in SUMMARY_ELEMENTS.items():
        for pri, name in enumerate(candidates):
            element_to_field[name] = (field, pri)
    for pri, name in enumerate(OP_INCOME_ELEMENTS):
        element_to_field[name] = ("op_income", pri)

    for r in rows:
        name = _local_name(r.get("要素ID", ""))
        if name not in element_to_field:
            continue
        ctx = _parse_context(r.get("コンテキストID", ""))
        if ctx is None:
            continue
        offset, _, member = ctx
        if member is None:
            consolidated = True
        elif member == "NonConsolidatedMember":
            consolidated = False
        else:
            continue  # 株主メンバー等、別用途のコンテキスト
        num = _to_number(r.get("値", ""))
        if num is None:
            continue
        field, pri = element_to_field[name]
        collected[(field, pri, offset, consolidated)] = num

    if not collected:
        return []

    # フィールドごとに「値が最も多く取れた候補」を採用し、連結優先で年度に展開
    years: Dict[int, Dict] = {}
    all_fields = list(SUMMARY_ELEMENTS) + ["op_income"]
    for field in all_fields:
        n_candidates = len(SUMMARY_ELEMENTS.get(field, OP_INCOME_ELEMENTS))
        best_pri, best_count = None, 0
        for pri in range(n_candidates):
            count = sum(1 for (f, p, _, _) in collected if f == field and p == pri)
            if count > best_count:
                best_pri, best_count = pri, count
        if best_pri is None:
            continue
        for offset in range(5):
            v = collected.get((field, best_pri, offset, True))
            if v is None:
                v = collected.get((field, best_pri, offset, False))
            if v is None:
                continue
            fy = _fiscal_year_label(period_end, offset)
            if fy is None:
                continue
            years.setdefault(offset, {"fiscal_year": fy, "is_forecast": 0})[field] = v

    out = [years[k] for k in sorted(years, reverse=True)]  # 古い期→新しい期

    # 比率系: 小数表記(|v|<=1)なら%に揃える(既存IR BANKデータと同じ単位に)
    for field in RATIO_FIELDS:
        vals = [r[field] for r in out if r.get(field) is not None]
        if vals and all(abs(v) <= 1.0 for v in vals):
            for r in out:
                if r.get(field) is not None:
                    r[field] = round(r[field] * 100, 2)

    for r in out:
        # 営業CFマージン(IR BANKは提供列、EDINETでは算出)
        if r.get("op_cf") is not None and r.get("revenue"):
            r["op_cf_margin"] = round(r["op_cf"] / r["revenue"] * 100, 2)
        for field in list(r):
            if field in INT_FIELDS and r[field] is not None:
                r[field] = int(r[field])
    return out


def parse_holders(rows: List[Dict[str, str]]) -> List[Dict]:
    """大株主の状況(当期末の上位10名)をholders相当のdictにする。"""
    period_end = next(
        (r["値"] for r in rows if _local_name(r.get("要素ID", "")) == PERIOD_END_ELEMENT),
        None)
    as_of = _fiscal_year_label(period_end, 0) if period_end else None
    if not as_of:
        return []

    names: Dict[int, str] = {}
    ratios: Dict[int, float] = {}
    for r in rows:
        element = _local_name(r.get("要素ID", ""))
        if element not in (HOLDER_NAME_ELEMENT, HOLDER_RATIO_ELEMENT):
            continue
        ctx = _parse_context(r.get("コンテキストID", ""))
        if ctx is None or ctx[2] is None:
            continue
        m = _HOLDER_MEMBER_RE.match(ctx[2])
        if not m or ctx[0] != 0:
            continue
        rank = int(m.group(1))
        if element == HOLDER_NAME_ELEMENT:
            name = r.get("値", "").strip()
            if name and name not in ("-", "−"):
                # 名前に併記される住所・改行を落とす(1行目のみ)
                names[rank] = name.splitlines()[0].strip()
        else:
            num = _to_number(r.get("値", ""))
            if num is not None:
                ratios[rank] = num

    if ratios and all(abs(v) <= 1.0 for v in ratios.values()):
        ratios = {k: round(v * 100, 2) for k, v in ratios.items()}

    out = []
    for rank in sorted(names):
        out.append({
            "as_of": as_of,
            "rank": rank,
            "holder_name": names[rank],
            "ratio": ratios.get(rank),
        })
    return out


def extract_report_csv(zip_bytes: bytes) -> Optional[bytes]:
    """書類CSVのZIPから本体(jpcrp*.csv)を取り出す。監査報告(jpaud*)は除く。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        candidates = [n for n in zf.namelist()
                      if n.lower().endswith(".csv") and "jpaud" not in n.lower()]
        if not candidates:
            return None
        # 本体は1つのはずだが、複数あれば最大のものを採る
        best = max(candidates, key=lambda n: zf.getinfo(n).file_size)
        return zf.read(best)


def parse_document(zip_bytes: bytes) -> Tuple[List[Dict], List[Dict]]:
    """書類ZIPから (financials行, holders行) を返す。"""
    csv_bytes = extract_report_csv(zip_bytes)
    if csv_bytes is None:
        return [], []
    rows = decode_csv(csv_bytes)
    return parse_financials(rows), parse_holders(rows)
