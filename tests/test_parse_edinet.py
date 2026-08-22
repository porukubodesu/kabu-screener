import io
import unittest
import zipfile

from src.parse_edinet import (
    decode_csv, extract_report_csv, parse_document, parse_financials, parse_holders,
)

HEADER = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別",
          "期間・時点", "ユニットID", "単位", "値"]


def make_csv(rows):
    """(要素ID, コンテキストID, 値) のリストをEDINET形式のCSVバイト列にする。"""
    lines = ["\t".join(f'"{c}"' for c in HEADER)]
    for element, context, value in rows:
        rec = [element, "項目", context, "当期", "連結", "時点", "JPY", "円", value]
        lines.append("\t".join(f'"{c}"' for c in rec))
    return "\n".join(lines).encode("utf-16")


def make_zip(csv_bytes, name="XBRL_TO_CSV/jpcrp030000-asr-001_E00000-000.csv"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, csv_bytes)
    return buf.getvalue()


PERIOD_END = ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31")


class TestParseFinancials(unittest.TestCase):
    def test_jgaap_summary_5years(self):
        rows = [PERIOD_END]
        for offset, prefix in enumerate(
                ["CurrentYear", "Prior1Year", "Prior2Year", "Prior3Year", "Prior4Year"]):
            v = 1000 - offset * 100  # 当期1000, 前期900, ...
            rows += [
                ("jpcrp_cor:NetSalesSummaryOfBusinessResults",
                 f"{prefix}Duration", str(v)),
                ("jpcrp_cor:OrdinaryIncomeLossSummaryOfBusinessResults",
                 f"{prefix}Duration", str(v // 10)),
                ("jpcrp_cor:TotalAssetsSummaryOfBusinessResults",
                 f"{prefix}Instant", str(v * 2)),
                ("jpcrp_cor:EquityToAssetRatioSummaryOfBusinessResults",
                 f"{prefix}Instant", "0.378"),
                ("jpcrp_cor:NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults",
                 f"{prefix}Duration", str(v // 5)),
                ("jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults",
                 f"{prefix}Instant", "50000"),
            ]
        fins = parse_financials(decode_csv(make_csv(rows)))

        self.assertEqual(len(fins), 5)
        self.assertEqual([r["fiscal_year"] for r in fins],
                         ["2022/03", "2023/03", "2024/03", "2025/03", "2026/03"])
        latest = fins[-1]
        self.assertEqual(latest["revenue"], 1000)
        self.assertEqual(latest["ordinary_income"], 100)
        self.assertEqual(latest["total_assets"], 2000)
        self.assertEqual(latest["equity_ratio"], 37.8)   # 小数 → %
        self.assertEqual(latest["op_cf"], 200)
        self.assertEqual(latest["op_cf_margin"], 20.0)   # op_cf/revenueで算出
        self.assertEqual(latest["shares_issued"], 50000)
        self.assertEqual(latest["is_forecast"], 0)

    def test_ratio_kept_when_already_percent(self):
        rows = [
            PERIOD_END,
            ("jpcrp_cor:EquityToAssetRatioSummaryOfBusinessResults",
             "CurrentYearInstant", "37.8"),
        ]
        fins = parse_financials(decode_csv(make_csv(rows)))
        self.assertEqual(fins[0]["equity_ratio"], 37.8)

    def test_ifrs_candidates(self):
        rows = [
            PERIOD_END,
            ("jpcrp_cor:RevenueIFRSSummaryOfBusinessResults",
             "CurrentYearDuration", "500"),
            ("jpcrp_cor:ProfitBeforeTaxIFRSSummaryOfBusinessResults",
             "CurrentYearDuration", "50"),
        ]
        fins = parse_financials(decode_csv(make_csv(rows)))
        self.assertEqual(fins[0]["revenue"], 500)
        self.assertEqual(fins[0]["ordinary_income"], 50)

    def test_consolidated_preferred_nonconsolidated_fallback(self):
        rows = [
            PERIOD_END,
            # 売上: 連結・個別の両方 → 連結を採用
            ("jpcrp_cor:NetSalesSummaryOfBusinessResults",
             "CurrentYearDuration", "1000"),
            ("jpcrp_cor:NetSalesSummaryOfBusinessResults",
             "CurrentYearDuration_NonConsolidatedMember", "800"),
            # 配当: 個別のみ → 個別を採用
            ("jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults",
             "CurrentYearDuration_NonConsolidatedMember", "30"),
        ]
        fins = parse_financials(decode_csv(make_csv(rows)))
        self.assertEqual(fins[0]["revenue"], 1000)
        self.assertEqual(fins[0]["dividend"], 30)

    def test_op_income_from_pl_statement(self):
        rows = [
            PERIOD_END,
            ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "1000"),
            ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "Prior1YearDuration", "900"),
            ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "120"),
            ("jppfs_cor:OperatingIncome", "Prior1YearDuration", "100"),
        ]
        fins = parse_financials(decode_csv(make_csv(rows)))
        by_fy = {r["fiscal_year"]: r for r in fins}
        self.assertEqual(by_fy["2026/03"]["op_income"], 120)
        self.assertEqual(by_fy["2025/03"]["op_income"], 100)

    def test_candidate_with_most_years_wins(self):
        # 優先度の高い候補が1期分しか無く、低い候補が3期分あるなら後者を採る
        rows = [
            PERIOD_END,
            ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "1"),
            ("jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults",
             "CurrentYearDuration", "100"),
            ("jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults",
             "Prior1YearDuration", "90"),
            ("jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults",
             "Prior2YearDuration", "80"),
        ]
        fins = parse_financials(decode_csv(make_csv(rows)))
        self.assertEqual(fins[-1]["revenue"], 100)

    def test_no_period_end_returns_empty(self):
        rows = [("jpcrp_cor:NetSalesSummaryOfBusinessResults",
                 "CurrentYearDuration", "1000")]
        self.assertEqual(parse_financials(decode_csv(make_csv(rows))), [])


class TestParseHolders(unittest.TestCase):
    def test_holders_with_fraction_ratio(self):
        rows = [
            PERIOD_END,
            ("jpcrp_cor:NameMajorShareholders",
             "CurrentYearInstant_No1MajorShareholdersMember", "山田 太郎"),
            ("jpcrp_cor:ShareholdingRatio",
             "CurrentYearInstant_No1MajorShareholdersMember", "0.128"),
            ("jpcrp_cor:NameMajorShareholders",
             "CurrentYearInstant_No2MajorShareholdersMember", "株式会社テスト銀行"),
            ("jpcrp_cor:ShareholdingRatio",
             "CurrentYearInstant_No2MajorShareholdersMember", "0.05"),
            # 前期のスナップショットは対象外
            ("jpcrp_cor:NameMajorShareholders",
             "Prior1YearInstant_No1MajorShareholdersMember", "昔の株主"),
        ]
        holders = parse_holders(decode_csv(make_csv(rows)))
        self.assertEqual(len(holders), 2)
        self.assertEqual(holders[0], {
            "as_of": "2026/03", "rank": 1, "holder_name": "山田 太郎", "ratio": 12.8})
        self.assertEqual(holders[1]["ratio"], 5.0)

    def test_percent_ratio_kept(self):
        rows = [
            PERIOD_END,
            ("jpcrp_cor:NameMajorShareholders",
             "CurrentYearInstant_No1MajorShareholdersMember", "山田 太郎"),
            ("jpcrp_cor:ShareholdingRatio",
             "CurrentYearInstant_No1MajorShareholdersMember", "12.8"),
        ]
        holders = parse_holders(decode_csv(make_csv(rows)))
        self.assertEqual(holders[0]["ratio"], 12.8)


class TestZipHandling(unittest.TestCase):
    def test_extract_skips_audit_csv(self):
        buf = io.BytesIO()
        body = make_csv([PERIOD_END])
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("XBRL_TO_CSV/jpaud-aar-cn-001_E00000-000.csv", b"x" * 10000)
            zf.writestr("XBRL_TO_CSV/jpcrp030000-asr-001_E00000-000.csv", body)
        self.assertEqual(extract_report_csv(buf.getvalue()), body)

    def test_parse_document_end_to_end(self):
        rows = [
            PERIOD_END,
            ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "1000"),
            ("jpcrp_cor:NameMajorShareholders",
             "CurrentYearInstant_No1MajorShareholdersMember", "山田 太郎"),
            ("jpcrp_cor:ShareholdingRatio",
             "CurrentYearInstant_No1MajorShareholdersMember", "0.3"),
        ]
        fins, holders = parse_document(make_zip(make_csv(rows)))
        self.assertEqual(fins[0]["revenue"], 1000)
        self.assertEqual(holders[0]["holder_name"], "山田 太郎")


if __name__ == "__main__":
    unittest.main()
