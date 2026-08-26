import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import report
from src.db import get_conn


def _seed(conn):
    conn.execute("INSERT INTO companies VALUES ('7203','トヨタ自動車','プライム','輸送用機器','TOPIX Core30','2026-08-22')")
    conn.execute("INSERT INTO companies VALUES ('9984','<スクリプト>テスト','プライム',NULL,NULL,'2026-08-22')")
    m1 = {"rev_cagr": 0.105, "consec_growth": 3, "op_cf_margin": 8.5,
          "equity_ratio": 38.4, "dilution": -0.01, "owner_ratio": 1.0,
          "owner_names": ["豊田 章男(0.5%)"], "has_vc": False, "vc_names": [],
          "business": "自動車の製造 <販売>"}
    m2 = {"rev_cagr": None, "consec_growth": 0, "op_cf_margin": None,
          "equity_ratio": None, "dilution": 2.1, "owner_ratio": 0,
          "owner_names": [], "has_vc": True, "vc_names": ["A&B投資事業有限責任組合"],
          "business": None}
    conn.execute("INSERT INTO screen_results VALUES ('2026-08-22','7203',1,0.912,?,NULL)",
                 (json.dumps(m1, ensure_ascii=False),))
    conn.execute("INSERT INTO screen_results VALUES ('2026-08-22','9984',2,0.5,?,NULL)",
                 (json.dumps(m2, ensure_ascii=False),))
    conn.execute("INSERT INTO financials(code, fiscal_year, source) VALUES ('7203','2026/03','edinet')")


class TestReport(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn(":memory:")
        _seed(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(report, "SITE_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        out = report.generate(self.conn, top=50)
        self.html = out.read_text(encoding="utf-8")

    def test_hero_is_rank1(self):
        self.assertIn("本日の1位", self.html)
        self.assertIn("トヨタ自動車", self.html)
        self.assertIn("0.912", self.html)

    def test_values_formatted(self):
        self.assertIn("10.5%", self.html)   # rev_cagr 0.105
        self.assertIn("8.5%", self.html)    # op_cf_marginは%値のまま
        self.assertIn("-1.0%", self.html)   # dilution signed

    def test_html_escaped(self):
        # 銘柄名・事業内容のHTMLはエスケープされる(XSS/レイアウト崩れ防止)
        self.assertNotIn("<スクリプト>", self.html)
        self.assertIn("&lt;スクリプト&gt;", self.html)
        self.assertIn("&lt;販売&gt;", self.html)

    def test_split_artifact_flagged_and_vc_chip(self):
        self.assertIn("分割?", self.html)   # dilution +210% には注記
        self.assertIn(">VC</span>", self.html)

    def test_empty_results_returns_none(self):
        conn = get_conn(":memory:")
        self.assertIsNone(report.generate(conn, top=50))


if __name__ == "__main__":
    unittest.main()
