import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import report
from src.db import get_conn
from src.fetch_prices import from_jquants_code, parse_daily_bars, to_jquants_code


def _seed(conn):
    conn.execute("INSERT INTO companies VALUES ('7203','トヨタ自動車','プライム','輸送用機器','TOPIX Core30','2026-08-22')")
    conn.execute("INSERT INTO companies VALUES ('9984','<スクリプト>テスト','プライム',NULL,NULL,'2026-08-22')")
    m1 = {"rev_cagr": 0.105, "consec_growth": 3, "op_cf_margin": 8.5,
          "equity_ratio": 38.4, "dilution": -0.01, "owner_ratio": 1.0,
          "owner_names": ["豊田 章男(0.5%)"], "has_vc": False, "vc_names": [],
          "business": "自動車の製造 <販売>", "holder_as_of": "2026/03"}
    m2 = {"rev_cagr": None, "consec_growth": 0, "op_cf_margin": None,
          "equity_ratio": None, "dilution": 2.1, "owner_ratio": 0,
          "owner_names": [], "has_vc": True, "vc_names": ["A&B投資事業有限責任組合"],
          "business": None}
    conn.execute("INSERT INTO screen_results VALUES ('2026-08-22','7203',1,0.912,?,NULL)",
                 (json.dumps(m1, ensure_ascii=False),))
    conn.execute("INSERT INTO screen_results VALUES ('2026-08-22','9984',2,0.5,?,NULL)",
                 (json.dumps(m2, ensure_ascii=False),))
    conn.execute("INSERT INTO financials(code, fiscal_year, source, revenue, op_income,"
                 " net_income, op_cf, net_assets, bps)"
                 " VALUES ('7203','2026/03','edinet', 50684952000000, 3766216000000,"
                 " 3848098000000, 5472920000000, 1000000000000, 1000)")
    conn.execute("INSERT INTO financials(code, fiscal_year, source, revenue, op_income)"
                 " VALUES ('7203','2025/03','edinet', 480367000000, 47955000000)")
    conn.execute("INSERT INTO financials(code, fiscal_year, is_forecast, source, revenue)"
                 " VALUES ('7203','2027/03', 1, 'irbank', 52000000000000)")
    conn.execute("INSERT INTO business VALUES ('7203', '3 【事業の内容】自動車事業を中心に'"
                 " || 'ロングテキスト' , '2026-03-31', '2026-08-22')")
    # 7203はmktcap無し(推定計算のフォールバック)、9984は公式mktcapあり
    conn.execute("INSERT INTO prices VALUES ('7203', '2026-08-27', 3000.0, NULL, '2026-08-28T07:00:00')")
    conn.execute("INSERT INTO prices VALUES ('9984', '2026-08-27', 100.0, 4500000000000.0, '2026-08-28T07:00:00')")


class TestReport(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn(":memory:")
        _seed(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(report, "SITE_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        out = report.generate(self.conn, top=100)
        self.html = out.read_text(encoding="utf-8")

    def test_rows_rendered_in_rank_order(self):
        self.assertIn("トヨタ自動車", self.html)
        self.assertIn("0.912", self.html)
        self.assertLess(self.html.find("トヨタ自動車"), self.html.find("&lt;スクリプト&gt;"))

    def test_values_formatted(self):
        self.assertIn("10.5%", self.html)    # rev_cagr 0.105
        self.assertIn("8.5%", self.html)     # op_cf_marginは%値のまま
        self.assertIn("-1.0%", self.html)    # dilution signed(パネルのメタ行)

    def test_html_escaped(self):
        # 銘柄名・事業内容のHTMLはエスケープされる(XSS/レイアウト崩れ防止)
        self.assertNotIn("<スクリプト>", self.html)
        self.assertIn("&lt;スクリプト&gt;", self.html)
        self.assertIn("&lt;販売&gt;", self.html)

    def test_split_artifact_flagged_and_vc_chip(self):
        self.assertIn("分割の境界誤差", self.html)   # dilution +210% には注記
        self.assertIn(">VC</span>", self.html)

    def test_logic_box_reflects_screen_constants(self):
        self.assertIn("ロジック", self.html)
        self.assertIn("営業CFの赤字なし", self.html)      # 有効なハードフィルタ
        self.assertIn("売上CAGR 30%", self.html)          # WEIGHTSから動的生成
        self.assertIn("パチンコ", self.html)              # NGワード一覧

    def test_fin_table_and_sparkbars(self):
        self.assertIn("50.68兆", self.html)      # 兆表記
        self.assertIn("4,804億", self.html)      # 億表記
        self.assertIn("2027/03(予)", self.html)  # 予想行のマーク
        self.assertIn('class="spark"', self.html)  # 実績2期以上でミニバー

    def test_sector_tabs(self):
        self.assertIn('data-sector="輸送用機器"', self.html)   # タブ+行の両方
        self.assertIn("全て", self.html)
        # sector33が無い銘柄はmarketで代替
        self.assertIn('data-sector="プライム"', self.html)

    def test_market_cap_from_price_and_shares(self):
        # mktcap無し: 株式数 = 純資産1e12/BPS1000 = 1e9株、終値3000円 → 3.00兆
        self.assertIn("3.00兆", self.html)
        # mktcapあり: 公式値4.5e12をそのまま使う
        self.assertIn("4.50兆", self.html)
        self.assertIn("終値 2026-08-27 時点", self.html)

    def test_chart_button_and_business_panel(self):
        self.assertIn('data-code="7203"', self.html)
        self.assertIn("月足チャート", self.html)
        self.assertIn("自動車事業を中心に", self.html)   # businessテーブル由来の全文スニペット

    def test_empty_results_returns_none(self):
        conn = get_conn(":memory:")
        self.assertIsNone(report.generate(conn, top=100))


class TestJquantsHelpers(unittest.TestCase):
    def test_code_mapping(self):
        # J-Quantsは5桁コード(4桁+0)。英字入りコードも同じ規則
        self.assertEqual(to_jquants_code("7203"), "72030")
        self.assertEqual(to_jquants_code("130A"), "130A0")
        self.assertEqual(from_jquants_code("72030"), "7203")
        self.assertEqual(from_jquants_code("130A0"), "130A")

    def test_parse_daily_bars(self):
        payload = {"data": [
            {"Code": "72030", "C": 2850.0, "AdjC": 2850.0, "MktCap": 45015714.0},
            {"Code": "130A0", "C": 500.0, "AdjC": None, "MktCap": None},  # 調整値なし→C
            {"Code": "99840", "C": None, "AdjC": None},                   # 値なし→捨てる
        ]}
        q = parse_daily_bars(payload)
        self.assertEqual(q["7203"], (2850.0, 45015714.0 * 1e6))  # MktCapは百万円→円
        self.assertEqual(q["130A"], (500.0, None))
        self.assertNotIn("9984", q)


if __name__ == "__main__":
    unittest.main()
