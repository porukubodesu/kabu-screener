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
    conn.execute("INSERT INTO financials(code, fiscal_year, source, revenue)"
                 " VALUES ('7203','2024/03','edinet', 400000000000)")
    conn.execute("INSERT INTO financials(code, fiscal_year, is_forecast, source, revenue)"
                 " VALUES ('7203','2027/03', 1, 'irbank', 52000000000000)")
    conn.execute("INSERT INTO business VALUES ('7203', '3 【事業の内容】自動車事業を中心に'"
                 " || '<試験>ロングテキスト。自社ブランドの商品を企画並びに販売。Z世代向け。'"
                 " , '2026-03-31', '2026-08-22')")
    # 7203はmktcap無し(推定計算のフォールバック)、9984は公式mktcapあり
    conn.execute("INSERT INTO prices VALUES ('7203', '2026-08-27', 3000.0, NULL, '2026-08-28T07:00:00')")
    conn.execute("INSERT INTO prices VALUES ('9984', '2026-08-27', 100.0, 4500000000000.0, '2026-08-28T07:00:00')")
    for d, o, h, l, c in [("2026-04-01", 2500, 2600, 2450, 2550),
                          ("2026-04-15", 2550, 2700, 2540, 2680),
                          ("2026-05-10", 2680, 2750, 2600, 2620),
                          ("2026-06-05", 2620, 2900, 2610, 2850)]:
        conn.execute("INSERT INTO price_bars VALUES ('7203', ?, ?, ?, ?, ?)",
                     (d, o, h, l, c))


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
        self.assertIn("&lt;試験&gt;", self.html)

    def test_business_heading_stripped(self):
        # 「3 【事業の内容】」の見出しゴミを落とし本文から始める
        self.assertNotIn("【事業の内容】", self.html)
        self.assertIn("自動車事業を中心に", self.html)

    def test_split_artifact_flagged_and_vc_chip(self):
        self.assertIn("分割境界", self.html)   # dilution +210% には注記(チップのtitle)
        self.assertIn(">VC</span>", self.html)

    def test_logic_box_reflects_screen_constants(self):
        self.assertIn("スクリーニングのロジック", self.html)
        self.assertIn("営業CFの赤字なし", self.html)      # 有効なハードフィルタ
        self.assertIn("売上CAGR 40%", self.html)          # WEIGHTSから動的生成
        self.assertNotIn("営業CFマージン", self.html)     # スコアから除外済み
        self.assertNotIn("非希薄化", self.html)
        self.assertIn("パチンコ", self.html)              # NGワード一覧

    def test_fin_table_and_sparkbars(self):
        self.assertIn("50.68兆", self.html)      # 兆表記
        self.assertIn("4,804億", self.html)      # 億表記
        self.assertIn("2027/03(予)", self.html)  # 予想行のマーク
        self.assertIn('class="spark"', self.html)  # 実績2期以上でミニバー

    def test_sector_tabs_and_cards(self):
        self.assertIn('data-tag="輸送用機器"', self.html)    # タブ
        self.assertIn('data-tags="輸送用機器"', self.html)   # カード
        self.assertIn("全て", self.html)
        # sector33が無い銘柄はmarketで代替
        self.assertIn('data-tags="プライム"', self.html)
        # カード型: クリック無しで決算・事業・大株主が最初から見える(hidden無し)
        self.assertIn('<article class="card"', self.html)
        self.assertNotIn("hidden", self.html.split("<article")[1].split("</article>")[0])

    def test_market_cap_from_price_and_shares(self):
        # mktcap無し: 株式数 = 純資産1e12/BPS1000 = 1e9株、終値3000円 → 3.00兆
        self.assertIn("3.00兆", self.html)
        # mktcapあり: 公式値4.5e12をそのまま使う
        self.assertIn("4.50兆", self.html)
        self.assertIn("終値 2026-08-27 時点", self.html)

    def test_candles_and_business(self):
        self.assertIn('class="candles"', self.html)     # 月足キャンドルSVG
        self.assertIn("月足3ヶ月", self.html)           # 4月・5月・6月の3本
        self.assertIn("自動車事業を中心に", self.html)   # businessテーブル由来の全文スニペット

    def test_monthly_aggregation(self):
        from src.report import monthly_candles
        m = monthly_candles([("2026-04-01", 2500, 2600, 2450, 2550),
                             ("2026-04-15", 2550, 2700, 2540, 2680)])
        # 始値=月初、高値=最大、安値=最小、終値=月末
        self.assertEqual(m, [("2026-04", 2500, 2700, 2450, 2680)])

    def test_empty_results_returns_none(self):
        conn = get_conn(":memory:")
        self.assertIsNone(report.generate(conn, top=100))


class TestBusinessExtraction(unittest.TestCase):
    def test_mission_and_market_noise_dropped(self):
        from src.report import extract_business
        text = ("当社は「世界を変える」というミッションを掲げております。"
                "近年、市場は拡大傾向にあります。"
                "当社はVTuberグループ「にじさんじ」を運営しております。")
        out = extract_business(text)
        self.assertIn("にじさんじ", out)
        self.assertNotIn("ミッション", out)
        self.assertNotIn("近年", out)

    def test_parenthetical_period_not_split(self):
        # 「(以下「DX」という。)」の句点で文を割らない(「)の推進…」断片を作らない)
        from src.report import extract_business
        text = ("デジタルトランスフォーメーション(以下「DX」という。)の推進を"
                "支援するサービスを提供しております。")
        out = extract_business(text)
        self.assertIn("(以下「DX」という。)の推進", out)
        self.assertFalse(out.startswith(")"))

    def test_fallback_when_nothing_concrete(self):
        from src.report import extract_business
        text = "特筆すべき記載はありません。"
        self.assertIn("特筆すべき", extract_business(text))  # 空にはしない

    def test_segments_extracted(self):
        from src.report import extract_segments
        text = ("当社は事業(以下「エンタメ・プラットフォーム事業」)と"
                "「エンタメ・コンテンツ事業」、および「新規事業」を営む。"
                "また「エンタメ・プラットフォーム事業」が主力である。")
        segs = extract_segments(text)
        # 重複なし・出現順・汎用語(新規事業)は除外
        self.assertEqual(segs, ["エンタメ・プラットフォーム事業", "エンタメ・コンテンツ事業"])


class TestThemes(unittest.TestCase):
    """教師データ(バイセル/パワーエックス/yutori型)と除外対象の分類テスト。"""

    def test_own_product_and_themes(self):
        from src.themes import MODEL_OWN, classify_model, match_themes
        buysell = "総合リユースサービスを提供。出張訪問買取事業と店舗買取事業。自社運営の販路。"
        self.assertEqual(classify_model(buysell), MODEL_OWN)
        self.assertIn("リユース・二次流通", match_themes(buysell))
        powerx = "蓄電池の開発、製造、販売から運用まで一貫して提供。エネルギー自給率の向上。"
        self.assertEqual(classify_model(powerx), MODEL_OWN)
        self.assertIn("エネルギー転換", match_themes(powerx))
        yutori = "自社ブランドの衣料品の企画並びに販売。Z世代を対象としたブランド運営。"
        self.assertEqual(classify_model(yutori), MODEL_OWN)
        self.assertIn("Z世代・新消費", match_themes(yutori))

    def test_contracted_and_real_estate_excluded(self):
        from src.themes import (MODEL_CONTRACTED, MODEL_REAL_ESTATE,
                                classify_model)
        consult = "コンサルティングサービスを提供し、システム開発の業務を受託しております。"
        self.assertEqual(classify_model(consult), MODEL_CONTRACTED)
        estate = "不動産の売買、不動産仲介および分譲マンションの開発を行う。"
        self.assertEqual(classify_model(estate), MODEL_REAL_ESTATE)

    def test_weak_signal_stays_other(self):
        from src.themes import MODEL_OTHER, classify_model, match_themes
        plain = "各種製品の製造販売を行っております。"
        self.assertEqual(classify_model(plain), MODEL_OTHER)  # 誤除外しない
        self.assertEqual(match_themes(plain), [])


class TestDiscover(unittest.TestCase):
    def test_discover_page(self):
        from src import discover
        conn = get_conn(":memory:")
        _seed(conn)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(discover, "SITE_DIR", Path(td)):
            out = discover.generate(conn, top=50)
            text = out.read_text(encoding="utf-8")
        # 3期以上の財務+自社ブランド+Z世代テーマを持つ7203が載る
        self.assertIn("発見機②", text)
        self.assertIn("トヨタ自動車", text)
        self.assertIn("Z世代・新消費", text)
        self.assertIn("CF赤字許容", text)


class TestBackfillMarker(unittest.TestCase):
    def test_recent_ipo_not_refetched(self):
        # 新規上場銘柄(履歴が2年に届かない)でも、一度バックフィルしたら再取得しない
        from datetime import date
        from src import fetch_prices
        conn = get_conn(":memory:")
        conn.execute("INSERT INTO screen_results VALUES ('2026-08-28','130A',1,0.9,'{}',NULL)")
        calls = []
        with mock.patch.object(fetch_prices, "fetch_code_bars",
                               side_effect=lambda *a: calls.append(a) or
                               [("2026-05-01", 1, 2, 0.5, 1.5)]), \
                mock.patch.object(fetch_prices.time, "sleep"):
            fetch_prices.backfill_bars(conn, None, "key", date(2026, 6, 5), top=300)
            fetch_prices.backfill_bars(conn, None, "key", date(2026, 6, 5), top=300)
        self.assertEqual(len(calls), 1)   # 2回目はマーカーでスキップ
        row = conn.execute("SELECT * FROM price_bars WHERE code='130A'").fetchone()
        self.assertEqual(row["date"], "2026-05-01")


class TestJquantsHelpers(unittest.TestCase):
    def test_code_mapping(self):
        # J-Quantsは5桁コード(4桁+0)。英字入りコードも同じ規則
        self.assertEqual(to_jquants_code("7203"), "72030")
        self.assertEqual(to_jquants_code("130A"), "130A0")
        self.assertEqual(from_jquants_code("72030"), "7203")
        self.assertEqual(from_jquants_code("130A0"), "130A")

    def test_parse_daily_bars(self):
        payload = {"data": [
            {"Code": "72030", "C": 2850.0, "AdjC": 2850.0, "MktCap": 45015714.0,
             "AdjO": 2872.0, "AdjH": 2885.5, "AdjL": 2837.0},
            {"Code": "130A0", "C": 500.0, "AdjC": None, "MktCap": None},  # 調整値なし→C
            {"Code": "99840", "C": None, "AdjC": None},                   # 値なし→捨てる
        ]}
        q = parse_daily_bars(payload)
        # (終値, 時価総額[百万円→円], 始値, 高値, 安値)
        self.assertEqual(q["7203"], (2850.0, 45015714.0 * 1e6, 2872.0, 2885.5, 2837.0))
        self.assertEqual(q["130A"][:2], (500.0, None))
        self.assertNotIn("9984", q)


if __name__ == "__main__":
    unittest.main()
