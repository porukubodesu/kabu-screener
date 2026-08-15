import unittest
from pathlib import Path

from src.parse_irbank import parse_fy_csv, parse_holder_html
from src.screen import (classify_holder, ng_business_keyword, _consec_increases,
                        _cagr, _snippet, percentile_map)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseFyCsv(unittest.TestCase):
    def setUp(self):
        text = (FIXTURES / "fy-data-all-7203.csv").read_text(encoding="utf-8-sig")
        self.rows = parse_fy_csv(text)

    def test_years_and_forecast(self):
        actual = [r for r in self.rows if not r["is_forecast"]]
        forecast = [r for r in self.rows if r["is_forecast"]]
        self.assertGreaterEqual(len(actual), 4)
        self.assertEqual(len(forecast), 1)
        self.assertEqual(forecast[0]["fiscal_year"], "2027/03")

    def test_sections_merged_by_year(self):
        fy2026 = next(r for r in self.rows if r["fiscal_year"] == "2026/03")
        self.assertEqual(fy2026["revenue"], 50684952000000)      # 業績
        self.assertEqual(fy2026["equity_ratio"], 37.8)           # 財務
        self.assertEqual(fy2026["op_cf"], 5472920000000)         # CF
        self.assertEqual(fy2026["dividend"], 95)                 # 配当

    def test_missing_values_are_absent(self):
        fy2026 = next(r for r in self.rows if r["fiscal_year"] == "2026/03")
        self.assertNotIn("ordinary_income", fy2026)  # トヨタの経常利益は'-'


class TestParseHolderHtml(unittest.TestCase):
    def setUp(self):
        text = (FIXTURES / "holder-7203.html").read_text(encoding="utf-8")
        self.rows = parse_holder_html(text)

    def test_latest_snapshot(self):
        latest = [r for r in self.rows if r["as_of"] == "2026/03"]
        self.assertGreaterEqual(len(latest), 5)
        top = next(r for r in latest if r["rank"] == 1)
        self.assertIn("マスタートラスト", top["holder_name"])
        self.assertAlmostEqual(top["ratio"], 12.8)

    def test_history_included(self):
        as_ofs = {r["as_of"] for r in self.rows}
        self.assertIn("2014/03", as_ofs)


class TestClassifyHolder(unittest.TestCase):
    def test_individual(self):
        for name in ("柳井正", "似鳥昭雄", "山田進太郎"):
            self.assertEqual(classify_holder(name), "individual")

    def test_corporate(self):
        for name in ("株式会社豊田自動織機", "日本生命保険相互会社",
                      "ステートストリートバンクアンドトラストカンパニー",
                      "BNYM AS AGT/CLTS NON TREATY JASDEC"):
            self.assertEqual(classify_holder(name), "corporate")

    def test_custodian(self):
        self.assertEqual(classify_holder("日本マスタートラスト信託銀行株式会社"), "custodian")
        self.assertEqual(classify_holder("株式会社日本カストディ銀行"), "custodian")

    def test_vc(self):
        self.assertEqual(classify_holder("ジャフコSV4共有投資事業有限責任組合"), "vc")
        self.assertEqual(classify_holder("グロービス5号ファンド投資事業有限責任組合"), "vc")


class TestNgBusiness(unittest.TestCase):
    def test_keyword_hit_returns_keyword(self):
        self.assertEqual(
            ng_business_keyword("当社はパチンコホールの運営を主たる事業とする"),
            "パチンコ")

    def test_no_hit_and_missing_pass(self):
        self.assertIsNone(ng_business_keyword("EC支援のSaaSを提供している"))
        self.assertIsNone(ng_business_keyword(None))  # 事業内容が未取得なら通す
        self.assertIsNone(ng_business_keyword(""))

    def test_snippet_flattens_and_truncates(self):
        self.assertEqual(_snippet("1行目\n2行目  詰める"), "1行目 2行目 詰める")
        self.assertEqual(_snippet("あ" * 130), "あ" * 120 + "…")
        self.assertIsNone(_snippet(None))


class TestMetricsHelpers(unittest.TestCase):
    def test_consec_increases(self):
        self.assertEqual(_consec_increases([1, 2, 3, 4]), 3)
        self.assertEqual(_consec_increases([5, 2, 3, 4]), 2)
        self.assertEqual(_consec_increases([1, 2, None, 4]), 0)  # 欠損があれば打ち切り
        self.assertEqual(_consec_increases([4, 3]), 0)

    def test_cagr(self):
        # 100→200 を4年なら年率18.9%
        self.assertAlmostEqual(
            _cagr(["2022/03", "2026/03"], [100.0, 200.0]), 0.1892, places=3)
        self.assertIsNone(_cagr(["2026/03"], [100.0]))

    def test_percentile_ties_get_same_value(self):
        pct = percentile_map({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0})
        self.assertEqual(pct["b"], pct["c"])
        self.assertEqual(pct["a"], 0.0)
        self.assertEqual(pct["d"], 1.0)
        self.assertAlmostEqual(pct["b"], 0.5)
        self.assertEqual(percentile_map({"x": 5.0}), {"x": 0.5})
        self.assertEqual(percentile_map({"x": None}), {})


if __name__ == "__main__":
    unittest.main()
