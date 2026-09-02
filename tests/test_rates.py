import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import rates
from src.db import get_conn


def _fin(conn, code, fy, **kw):
    cols = ["code", "fiscal_year", "source"] + list(kw)
    vals = [code, fy, "edinet"] + list(kw.values())
    conn.execute(f"INSERT INTO financials({','.join(cols)}) VALUES ({','.join('?' * len(vals))})", vals)


def _seed(conn):
    rows = [
        # code, name, sector33, scale
        ("8331", "千葉銀行", "銀行業", "TOPIX Mid400"),
        ("8306", "メガ銀行", "銀行業", "TOPIX Core30"),
        ("8750", "第一生命ＨＤ", "保険業", "TOPIX Large70"),
        ("8697", "日本取引所グループ", "その他金融業", "TOPIX Large70"),
        ("8604", "野村ＨＤ", "証券、商品先物取引業", "TOPIX Large70"),
        ("8572", "消費者金融Ａ", "その他金融業", "TOPIX Mid400"),
        ("7974", "任天堂", "その他製品", "TOPIX Core30"),
        ("9999", "借金会社", "機械", "TOPIX Small 2"),
        ("8801", "自己資本デベ", "不動産業", "TOPIX Large70"),
        ("8802", "借入デベ", "不動産業", "TOPIX Mid400"),
    ]
    for code, name, sector, scale in rows:
        conn.execute("INSERT INTO companies VALUES (?,?,'プライム',?,?,'2026-09-01')",
                     (code, name, sector, scale))
    # 経常利益(億円)の推移 2022/03..2026/03 と ROE
    ords = {
        "8331": ([900, 950, 1000, 1200, 1500], 6.0),   # 2024→2026 +50%、4期連続
        "8306": ([18000, 19000, 20000, 21000, 22000], 8.0),  # +10%
        "8750": ([3000, 2800, 3000, 3300, 3600], 7.0),
        "8697": ([700, 720, 800, 900, 1000], 15.0),
        "8604": ([500, 1000, 1500, 1600, 1700], 9.0),
        "8572": ([400, 400, 400, 400, 400], 10.0),
        "7974": ([5000, 6000, 6800, 4000, 5000], 12.0),
        "9999": ([10, 10, 10, 10, 10], 3.0),
        "8801": ([2500, 2600, 2700, 2800, 3000], 8.0),
        "8802": ([500, 550, 600, 620, 650], 10.0),   # +8%(8801の+11%より低い)
    }
    for code, (vals, roe) in ords.items():
        for fy, v in zip(["2022/03", "2023/03", "2024/03", "2025/03", "2026/03"], vals):
            kw = dict(revenue=v * 10 * 1e8, op_income=v * 1e8, ordinary_income=v * 1e8,
                      net_income=v * 0.6 * 1e8, roe=roe, op_cf=v * 1e8)
            if fy == "2026/03":
                if code == "7974":   # 現預金2兆・総資産3兆・純資産2.6兆 → ネットキャッシュ1.6兆
                    kw.update(cash=2e12, total_assets=3e12, net_assets=2.6e12,
                              net_income=3e11, bps=2600.0, equity_ratio=86.7)
                elif code == "9999":  # 現預金100億・総負債800億 → ネットキャッシュ負
                    kw.update(cash=1e10, total_assets=1e11, net_assets=2e10,
                              bps=100.0, equity_ratio=20.0)
                elif code == "8801":
                    kw.update(net_assets=3e12, bps=3000.0, equity_ratio=35.0)
                elif code == "8802":
                    kw.update(net_assets=5e11, bps=500.0, equity_ratio=15.0)
                elif code == "8331":
                    kw.update(net_assets=1.5e12, bps=1500.0, equity_ratio=5.0)
                else:
                    kw.update(net_assets=v * 20 * 1e8, bps=1000.0, equity_ratio=10.0)
            _fin(conn, code, fy, **kw)
    conn.execute("INSERT INTO prices VALUES ('8331','2026-06-05',1200.0,1.2e12,'2026-09-01')")
    conn.execute("INSERT INTO prices VALUES ('7974','2026-06-05',10000.0,1e13,'2026-09-01')")
    conn.execute("INSERT INTO business VALUES ('8572','消費者金融事業を営む','2026-03-31','2026-09-01')")


class TestBuckets(unittest.TestCase):
    def test_bucket_of(self):
        self.assertEqual(rates.bucket_of("銀行業", "千葉銀行"), rates.BUCKET_BANK)
        self.assertEqual(rates.bucket_of("保険業", "x"), rates.BUCKET_INSURANCE)
        self.assertEqual(rates.bucket_of("証券、商品先物取引業", "x"), rates.BUCKET_SECURITIES)
        self.assertEqual(rates.bucket_of("その他金融業", "日本取引所グループ"), rates.BUCKET_SECURITIES)
        self.assertIsNone(rates.bucket_of("その他金融業", "オリックス"))  # リース等は対象外
        self.assertEqual(rates.bucket_of("不動産業", "x"), rates.BUCKET_REAL_ESTATE)
        self.assertEqual(rates.bucket_of("機械", "x"), rates.BUCKET_NET_CASH)
        self.assertEqual(rates.bucket_of(None, "x"), rates.BUCKET_NET_CASH)

    def test_rate_cycle_growth(self):
        rows = [{"fiscal_year": "2024/03", "ordinary_income": 100},
                {"fiscal_year": "2025/03", "ordinary_income": 120},
                {"fiscal_year": "2026/03", "ordinary_income": 150}]
        self.assertAlmostEqual(rates.rate_cycle_growth(rows), 0.5)
        # 直近が2024年度以前 / 起点欠損 / 赤字 → None
        self.assertIsNone(rates.rate_cycle_growth(rows[:1]))
        self.assertIsNone(rates.rate_cycle_growth(rows[1:]))
        self.assertIsNone(rates.rate_cycle_growth(
            [{"fiscal_year": "2024/03", "ordinary_income": -5}, rows[2]]))

    def test_net_cash_metrics(self):
        rows = [{"fiscal_year": "2025/03", "cash": 1, "total_assets": None, "net_assets": 1},
                {"fiscal_year": "2026/03", "cash": 2e12, "total_assets": 3e12,
                 "net_assets": 2.6e12, "net_income": 3e11}]
        m = rates.net_cash_metrics(rows)
        self.assertEqual(m["net_cash"], 1.6e12)
        self.assertEqual(m["net_cash_fy"], "2026/03")      # 3値が揃う年度を使う
        self.assertAlmostEqual(m["rate_uplift"], 1.6e12 * 0.01 / 3e11)
        self.assertEqual(rates.net_cash_metrics([rows[0]]), {})


class TestRatesPage(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn(":memory:")
        _seed(self.conn)
        self.rows = rates.build_rate_rows(self.conn, top=50)
        self.by_code = {r["code"]: r for r in self.rows}

    def _bucket(self, bucket):
        return [r["code"] for r in self.rows if r["metrics"]["themes"][0] == bucket]

    def test_buckets_and_ranks(self):
        self.assertEqual(self._bucket(rates.BUCKET_BANK), ["8331", "8306"])  # 実測増益率+50%が先
        self.assertEqual(self.by_code["8331"]["rank"], 1)
        self.assertEqual(self.by_code["8306"]["rank"], 2)   # rankは分類内
        self.assertEqual(self._bucket(rates.BUCKET_INSURANCE), ["8750"])
        self.assertEqual(set(self._bucket(rates.BUCKET_SECURITIES)), {"8697", "8604"})
        self.assertEqual(self._bucket(rates.BUCKET_NET_CASH), ["7974"])   # 借金会社は除外
        self.assertEqual(self._bucket(rates.BUCKET_REAL_ESTATE), ["8801", "8802"])
        self.assertNotIn("8572", self.by_code)   # NGワード(消費者金融)+その他金融業
        # 分類順に並ぶ(銀行→保険→証券→ネットキャッシュ→不動産)
        order = [r["metrics"]["themes"][0] for r in self.rows]
        self.assertEqual(order, sorted(order, key=rates.BUCKETS.index))

    def test_metrics(self):
        m = self.by_code["8331"]["metrics"]
        self.assertAlmostEqual(m["rate_cycle_growth"], 0.5)
        self.assertEqual(m["consec_ord_up"], 4)
        self.assertAlmostEqual(m["pbr"], 0.8)             # 時価総額1.2兆 / 純資産1.5兆
        n = self.by_code["7974"]["metrics"]
        self.assertEqual(n["net_cash"], 1.6e12)
        self.assertAlmostEqual(n["net_cash_to_mcap"], 0.16)
        self.assertAlmostEqual(n["rate_uplift"], 0.0533, places=3)

    def test_reference_ranks(self):
        lines = rates.reference_ranks(self.rows)
        self.assertIn("8331 千葉銀行: ① 銀行 #1", "\n".join(lines))
        self.assertIn("6954 ファナック: 圏外", "\n".join(lines))

    def test_generate_html(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(rates, "SITE_DIR", Path(td)):
            out = rates.generate(self.conn, top=50)
            html = out.read_text(encoding="utf-8")
        self.assertIn("発見機③", html)
        self.assertIn('data-tag="① 銀行"', html)           # 分類タブ
        tabs = html.split('<div class="tabs">')[1].split("</div>")[0]
        self.assertEqual([b for b in rates.BUCKETS if b in tabs],   # タブは分類順(件数順ではない)
                         sorted(rates.BUCKETS, key=tabs.find))
        self.assertLess(html.find("千葉銀行"), html.find("メガ銀行"))
        self.assertIn("PBR <b>0.80倍</b>", html)
        self.assertIn("2024年度→直近 <b>+50%</b>", html)
        self.assertIn("規模 <b>TOPIX Mid400</b>", html)
        self.assertIn("ネットキャッシュ(2026/03) <b>1.60兆</b>", html)
        self.assertIn("金利+1%の利益押上げ <b>+5.3%</b>", html)
        self.assertNotIn("借金会社", html)
        self.assertNotIn("消費者金融Ａ", html)
        # 他ページへのナビは2つ
        self.assertIn('href="index.html"', html)
        self.assertIn('href="discover.html"', html)
        self.assertIn("推奨ではなく研究対象", html)

    def test_empty_db(self):
        self.assertIsNone(rates.generate(get_conn(":memory:"), top=50))


if __name__ == "__main__":
    unittest.main()
