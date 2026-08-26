import codecs
import io
import sqlite3
import tempfile
import unittest
import zipfile

import requests
from datetime import date
from pathlib import Path
from unittest import mock

from src import fetch_edinet
from src.db import get_conn
from src.parse_edinet import (parse_business, parse_financials, parse_holders,
                              read_jpcrp_rows)

FIXTURES = Path(__file__).parent / "fixtures"
PERIOD_END = "2026-03-31"


def build_zip(csv_text: str) -> bytes:
    """実際の type=5 レスポンスと同じ構成のzipを作る(UTF-16LE+BOM、タブ区切り)。"""
    body = codecs.BOM_UTF16_LE + csv_text.encode("utf-16-le")
    decoy = codecs.BOM_UTF16_LE + (
        '"要素ID"\t"項目名"\t"コンテキストID"\t"相対年度"\t"連結・個別"\t'
        '"期間・時点"\t"ユニットID"\t"単位"\t"値"\r\n'
        '"jpcrp_cor:NetSalesSummaryOfBusinessResults"\t"売上高"\t'
        '"CurrentYearDuration"\t"当期"\t"連結"\t"期間"\t"JPY"\t"円"\t"777"\r\n'
    ).encode("utf-16-le")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "XBRL_TO_CSV/jpcrp030000-asr-001_E00001-000_2026-03-31_01_2026-06-25.csv",
            body)
        # 監査報告書は対象外(誤って読むと売上777が混入する)
        zf.writestr(
            "XBRL_TO_CSV/jpaud-aai-cc-001_E00001-000_2026-03-31_01_2026-06-25.csv",
            decoy)
    return buf.getvalue()


class TestParseEdinet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        cls.rows = read_jpcrp_rows(build_zip(text))
        cls.fins = parse_financials(cls.rows, PERIOD_END)
        cls.by_fy = {r["fiscal_year"]: r for r in cls.fins}

    def test_five_years_from_summary(self):
        self.assertEqual([r["fiscal_year"] for r in self.fins],
                         ["2022/03", "2023/03", "2024/03", "2025/03", "2026/03"])
        self.assertTrue(all(r["is_forecast"] == 0 for r in self.fins))
        self.assertEqual(self.by_fy["2022/03"]["revenue"], 1100000000)
        self.assertEqual(self.by_fy["2026/03"]["revenue"], 1500000000)

    def test_consolidated_wins_over_nonconsolidated(self):
        # 個別の売上999000000・営業利益100000000で上書きされないこと
        self.assertEqual(self.by_fy["2026/03"]["revenue"], 1500000000)
        self.assertEqual(self.by_fy["2026/03"]["op_income"], 140000000)

    def test_op_income_from_statements(self):
        # 営業利益はサマリーに無く財務諸表本体から当期+前期のみ
        self.assertEqual(self.by_fy["2026/03"]["op_income"], 140000000)
        self.assertEqual(self.by_fy["2025/03"]["op_income"], 130000000)
        self.assertNotIn("op_income", self.by_fy["2024/03"])

    def test_segment_rows_ignored(self):
        # セグメント情報(Member付きコンテキスト)の88000000がどこにも入らないこと
        for r in self.fins:
            self.assertNotEqual(r.get("op_income"), 88000000)

    def test_ratio_converted_to_percent(self):
        self.assertAlmostEqual(self.by_fy["2026/03"]["equity_ratio"], 40.0)
        self.assertAlmostEqual(self.by_fy["2026/03"]["roe"], 12.5)
        self.assertAlmostEqual(self.by_fy["2026/03"]["payout_ratio"], 24.9)

    def test_eps_excludes_diluted(self):
        self.assertAlmostEqual(self.by_fy["2026/03"]["eps"], 100.5)

    def test_dividend_from_nonconsolidated_excludes_interim(self):
        self.assertAlmostEqual(self.by_fy["2026/03"]["dividend"], 25.0)

    def test_op_cf_margin_computed(self):
        # 120000000 / 1500000000 * 100 = 8.0
        self.assertAlmostEqual(self.by_fy["2026/03"]["op_cf_margin"], 8.0)
        self.assertAlmostEqual(self.by_fy["2025/03"]["op_cf_margin"], 7.86)

    def test_missing_value_absent(self):
        self.assertNotIn("ordinary_income", self.by_fy["2024/03"])  # 値が「－」

    def test_shares_and_instant_fields(self):
        self.assertEqual(self.by_fy["2026/03"]["shares_issued"], 1000000)
        self.assertEqual(self.by_fy["2026/03"]["net_assets"], 800000000)
        self.assertEqual(self.by_fy["2025/03"]["net_assets"], 700000000)
        self.assertAlmostEqual(self.by_fy["2026/03"]["bps"], 804.0)

    def test_audit_csv_ignored(self):
        self.assertNotEqual(self.by_fy["2026/03"]["revenue"], 777)


class TestParseHolders(unittest.TestCase):
    def setUp(self):
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        self.holders = parse_holders(read_jpcrp_rows(build_zip(text)), PERIOD_END)

    def test_holders(self):
        self.assertEqual(len(self.holders), 3)
        top = self.holders[0]
        self.assertEqual(top["as_of"], "2026/03")
        self.assertEqual(top["rank"], 1)
        self.assertEqual(top["holder_name"], "山田 太郎")
        self.assertAlmostEqual(top["ratio"], 35.0)
        self.assertEqual(self.holders[1]["holder_name"], "エディネット株式会社")
        self.assertAlmostEqual(self.holders[1]["ratio"], 10.1)

    def test_fullwidth_name_normalized(self):
        # 全角ＥＤＩＮＥＴ→半角EDINET(classify_holderの英字マーカーに当たるように)
        self.assertEqual(self.holders[2]["holder_name"],
                         "EDINETキャピタル投資事業有限責任組合")

    def test_vc_name_survives_for_classification(self):
        from src.screen import classify_holder
        self.assertEqual(classify_holder(self.holders[2]["holder_name"]), "vc")


class TestConsolidatedIfrs(unittest.TestCase):
    """連結IFRS銘柄: 親会社帰属利益の優先と、単体値の混入防止。"""

    def _parse(self, csv_text):
        return parse_financials(read_jpcrp_rows(build_zip(csv_text)), PERIOD_END)

    def test_parent_attributable_profit_wins_regardless_of_order(self):
        # ラベルは「当期利益又は当期損失(△):親会社の所有者に帰属」の語順なので
        # キーワードでは拾えず、要素ID(ProfitLossAttributableToOwnersOfParent)で勝つ
        fins = self._parse(IFRS_CSV)
        self.assertEqual(fins[0]["net_income"], 450)  # 非支配込み500ではない
        # 行順を逆にしても親会社帰属が勝つ
        header, *body = IFRS_CSV.split("\r\n")
        fins = self._parse("\r\n".join([header] + body[::-1]))
        self.assertEqual(fins[0]["net_income"], 450)

    def test_ifrs_equity_and_bps_for_dilution(self):
        # 希薄化計算の分子・分母(親会社帰属持分と1株当たり親会社所有者帰属持分)が
        # 取れること。PerShare要素が持分(net_assets)に混入しないこと
        fins = self._parse(IFRS_CSV)
        self.assertEqual(fins[0]["net_assets"], 2000)
        self.assertAlmostEqual(fins[0]["bps"], 20.0)

    def test_nonconsolidated_op_income_dropped_for_consolidated_filer(self):
        # 連結売上とスケールの合わない単体営業利益(40)を混ぜない
        fins = self._parse(IFRS_CSV)
        self.assertEqual(fins[0]["revenue"], 1000)
        self.assertNotIn("op_income", fins[0])

    def test_extension_element_with_empty_label(self):
        # トヨタ実データ型: 連結売上が企業固有拡張要素(KeyFinancialData、ラベル空)
        # でのみ出るケース。要素IDで拾え、単体の売上高(標準要素)に負けないこと
        fins = self._parse(TOYOTA_CSV)
        self.assertEqual(fins[0]["revenue"], 50684952000000)

    def test_nonconsolidated_filer_still_uses_parent_values(self):
        solo = CSV_HEADER + (
            '"jpcrp_cor:NetSalesSummaryOfBusinessResults"\t"売上高"\t'
            '"CurrentYearDuration_NonConsolidatedMember"\t"当期"\t"個別"\t'
            '"期間"\t"JPY"\t"円"\t"800"\r\n'
            '"jppfs_cor:OperatingIncome"\t"営業利益"\t'
            '"CurrentYearDuration_NonConsolidatedMember"\t"当期"\t"個別"\t'
            '"期間"\t"JPY"\t"円"\t"80"\r\n'
        )
        fins = self._parse(solo)
        self.assertEqual(fins[0]["revenue"], 800)
        self.assertEqual(fins[0]["op_income"], 80)


class TestParseBusiness(unittest.TestCase):
    def test_html_stripped_and_normalized(self):
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        desc = parse_business(read_jpcrp_rows(build_zip(text)))
        # タグ除去+ブロック要素の境界は改行、全角英数はNFKCで半角、実体参照は解決
        self.assertIn("3【事業の内容】\n当社グループは、EC支援の SaaS を提供している。", desc)
        self.assertIn("主な顧客は&中小企業。\n単一セグメントである。", desc)
        self.assertNotIn("<", desc)

    def test_absent_returns_none(self):
        self.assertIsNone(parse_business(read_jpcrp_rows(build_zip(MERGE_CSV))))


class TestApiErrorDetection(unittest.TestCase):
    """認証エラーはHTTP 200のJSONで返る(実測)ので、空応答と区別して中断できること。"""

    def test_doc_fetch_error_json_raises(self):
        fake = mock.Mock(content=b'{"StatusCode": 401,"message": "Access denied"}')
        with mock.patch.object(fetch_edinet, "_get", return_value=fake):
            with self.assertRaises(fetch_edinet.ApiError):
                fetch_edinet.fetch_doc_csv(None, "S100TEST", "key", 0)

    def test_list_error_raises(self):
        fake = mock.Mock()
        fake.json.return_value = {"StatusCode": 401, "message": "Access denied"}
        with mock.patch.object(fetch_edinet, "_get", return_value=fake):
            with self.assertRaises(fetch_edinet.ApiError):
                fetch_edinet.list_documents(None, date(2026, 7, 1), "key", 0)

    def test_request_errors_redact_api_key(self):
        # 通信エラーの例外文字列はキー入りURLを含む。daily.logに出るため伏せること
        err = requests.exceptions.ConnectionError(
            "pool: /api/v2/documents.json?Subscription-Key=SECRET123&type=2 failed")
        session = mock.Mock()
        session.get.side_effect = err
        with self.assertRaises(RuntimeError) as cm:
            fetch_edinet._get(session, "https://api/documents.json", {}, 0)
        self.assertNotIn("SECRET123", str(cm.exception))
        self.assertIn("Subscription-Key=***", str(cm.exception))

    def test_doc_fetch_404_json_returns_none(self):
        fake = mock.Mock(content=b'{"StatusCode": 404,"message": "Not Found"}')
        with mock.patch.object(fetch_edinet, "_get", return_value=fake):
            self.assertIsNone(fetch_edinet.fetch_doc_csv(None, "S100TEST", "key", 0))


DOC = {"docID": "S100TEST", "docTypeCode": "120", "periodEnd": "2026-03-31",
       "submitDateTime": "2026-06-25 15:00"}

CSV_HEADER = ('"要素ID"\t"項目名"\t"コンテキストID"\t"相対年度"\t"連結・個別"\t'
              '"期間・時点"\t"ユニットID"\t"単位"\t"値"\r\n')

# 2回目の取り込み(翌年の有報を想定): 純利益だけ更新が来るケース
MERGE_CSV = CSV_HEADER + (
    '"jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults"\t'
    '"親会社株主に帰属する当期純利益"\t"CurrentYearDuration"\t"当期"\t"連結"\t'
    '"期間"\t"JPY"\t"円"\t"123456789"\r\n'
)

# トヨタ型: 連結売上が拡張要素(SummaryOfBusinessResultsでなくKeyFinancialData、
# ラベル列は空)でタグ付けされ、標準要素の売上高は単体のみのケース
TOYOTA_CSV = CSV_HEADER + (
    '"jpcrp030000-asr_E00001-000:OperatingRevenuesIFRSKeyFinancialData"\t""\t'
    '"CurrentYearDuration"\t"当期"\t"その他"\t"期間"\t"JPY"\t"円"\t"50684952000000"\r\n'
    '"jpcrp_cor:NetSalesSummaryOfBusinessResults"\t"売上高、経営指標等"\t'
    '"CurrentYearDuration_NonConsolidatedMember"\t"当期"\t"その他"\t"期間"\t"JPY"\t'
    '"円"\t"18259979000000"\r\n'
)

# 連結IFRS銘柄: 非支配持分込みの当期利益と親会社帰属(ラベルは語順の異なる表記)が
# 併存し、連結の営業利益は無く単体(jppfs)の営業利益だけがあるケース。
# 持分・BPSも親会社帰属のIFRSラベル
IFRS_CSV = CSV_HEADER + (
    '"jpcrp_cor:ProfitLossAttributableToNonControllingInterestsIFRSSummaryOfBusinessResults"\t'
    '"非支配持分に帰属する当期利益"\t'
    '"CurrentYearDuration"\t"当期"\t"連結"\t"期間"\t"JPY"\t"円"\t"50"\r\n'
    '"jpcrp_cor:RevenueIFRSSummaryOfBusinessResults"\t"売上収益"\t'
    '"CurrentYearDuration"\t"当期"\t"連結"\t"期間"\t"JPY"\t"円"\t"1000"\r\n'
    '"jpcrp_cor:ProfitLossIFRSSummaryOfBusinessResults"\t"当期利益"\t'
    '"CurrentYearDuration"\t"当期"\t"連結"\t"期間"\t"JPY"\t"円"\t"500"\r\n'
    '"jpcrp_cor:ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults"\t'
    '"当期利益又は当期損失（△）：親会社の所有者に帰属"\t"CurrentYearDuration"\t"当期"\t"連結"\t'
    '"期間"\t"JPY"\t"円"\t"450"\r\n'
    '"jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults"\t'
    '"親会社の所有者に帰属する持分"\t"CurrentYearInstant"\t"当期末"\t"連結"\t'
    '"時点"\t"JPY"\t"円"\t"2000"\r\n'
    '"jpcrp_cor:EquityToEquityAttributableToOwnersOfParentPerShareIFRSSummaryOfBusinessResults"\t'
    '"１株当たり親会社所有者帰属持分"\t"CurrentYearInstant"\t"当期末"\t"連結"\t'
    '"時点"\t"JPYPerShares"\t"円/株"\t"20.0"\r\n'
    '"jppfs_cor:OperatingIncome"\t"営業利益"\t"CurrentYearDuration_NonConsolidatedMember"\t'
    '"当期"\t"個別"\t"期間"\t"JPY"\t"円"\t"40"\r\n'
)


def business_csv(desc_html: str) -> str:
    return CSV_HEADER + (
        f'"jpcrp_cor:DescriptionOfBusinessTextBlock"\t"事業の内容"\t'
        f'"FilingDateInstant"\t"提出日時点"\t"連結"\t"時点"\t""\t""\t"{desc_html}"\r\n'
    )


class TestSaveDocument(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn(":memory:")
        # IR BANK由来の行を仕込む: EDINETの5年窓の外の古い実績 / 窓内の実績 / 予想
        self.conn.execute(
            "INSERT INTO financials(code, fiscal_year, is_forecast, revenue, op_income)"
            " VALUES ('7203', '2021/03', 0, 999, 99)")
        self.conn.execute(
            "INSERT INTO financials(code, fiscal_year, is_forecast, revenue, capex)"
            " VALUES ('7203', '2025/03', 0, 111, 55)")
        self.conn.execute(
            "INSERT INTO financials(code, fiscal_year, is_forecast, revenue)"
            " VALUES ('7203', '2027/03', 1, 222)")
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(fetch_edinet, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _rows(self):
        return self.conn.execute(
            "SELECT * FROM financials WHERE code='7203' ORDER BY fiscal_year"
        ).fetchall()

    def test_merges_with_irbank_history(self):
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        fin, holders = fetch_edinet.save_document(
            self.conn, "7203", DOC, build_zip(text))
        self.assertEqual((fin, holders), (5, 3))
        rows = self._rows()
        # 5年窓の外の2021/03と予想2027/03は残り、窓内はEDINETに置き換わる
        self.assertEqual([r["fiscal_year"] for r in rows],
                         ["2021/03", "2022/03", "2023/03", "2024/03",
                          "2025/03", "2026/03", "2027/03"])
        by_fy = {r["fiscal_year"]: r for r in rows}
        self.assertEqual(by_fy["2021/03"]["source"], "irbank")  # 古い履歴は保全
        self.assertEqual(by_fy["2021/03"]["op_income"], 99)
        self.assertEqual(by_fy["2025/03"]["source"], "edinet")  # 重なる年度はEDINET優先
        self.assertEqual(by_fy["2025/03"]["revenue"], 1400000000)
        self.assertEqual(by_fy["2025/03"]["capex"], 55)  # EDINETに無い列はIR BANK値が残る
        self.assertEqual(by_fy["2027/03"]["is_forecast"], 1)  # 予想行は無関係なので残る
        self.assertEqual(by_fy["2026/03"]["revenue"], 1500000000)
        doc_row = self.conn.execute("SELECT * FROM edinet_docs").fetchone()
        self.assertEqual((doc_row["doc_id"], doc_row["status"]), ("S100TEST", "ok"))

    def test_same_as_of_holders_replaced(self):
        # 同一時点の古いスナップショット(退出済みVC)は消え、他時点は残る
        self.conn.execute(
            "INSERT INTO holders(code, as_of, rank, holder_name, ratio)"
            " VALUES ('7203', '2026/03', 1, '退出済ベンチャーキャピタル', 20.0)")
        self.conn.execute(
            "INSERT INTO holders(code, as_of, rank, holder_name, ratio)"
            " VALUES ('7203', '2025/09', 1, '山田 太郎', 34.0)")
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        fetch_edinet.save_document(self.conn, "7203", DOC, build_zip(text))
        latest = [r["holder_name"] for r in self.conn.execute(
            "SELECT holder_name FROM holders WHERE code='7203' AND as_of='2026/03'")]
        self.assertNotIn("退出済ベンチャーキャピタル", latest)
        self.assertEqual(len(latest), 3)
        old = self.conn.execute(
            "SELECT COUNT(*) FROM holders WHERE code='7203' AND as_of='2025/09'"
        ).fetchone()[0]
        self.assertEqual(old, 1)

    def test_forecast_row_fully_replaced(self):
        # EDINET実績と同年度にIR BANK予想行があるとき、EDINETに無いフィールドの
        # 予想値(capex=77)が実績としてすり替わらないこと
        self.conn.execute(
            "INSERT INTO financials(code, fiscal_year, is_forecast, revenue, capex)"
            " VALUES ('7203', '2026/03', 1, 222, 77)")
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        fetch_edinet.save_document(self.conn, "7203", DOC, build_zip(text))
        row = self.conn.execute(
            "SELECT * FROM financials WHERE code='7203' AND fiscal_year='2026/03'"
        ).fetchone()
        self.assertEqual(row["is_forecast"], 0)
        self.assertEqual(row["revenue"], 1500000000)
        self.assertIsNone(row["capex"])

    def test_business_saved_newer_wins_older_ignored(self):
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        fetch_edinet.save_document(self.conn, "7203", DOC, build_zip(text))
        row = self.conn.execute("SELECT * FROM business WHERE code='7203'").fetchone()
        self.assertIn("EC支援", row["description"])
        self.assertEqual(row["period_end"], "2026-03-31")
        # 期末が古い有報(遅延提出)では上書きされない
        old_doc = dict(DOC, docID="S100OLD1", periodEnd="2025-03-31")
        fetch_edinet.save_document(
            self.conn, "7203", old_doc, build_zip(business_csv("<p>倉庫業</p>")))
        row = self.conn.execute("SELECT * FROM business WHERE code='7203'").fetchone()
        self.assertIn("EC支援", row["description"])
        self.assertEqual(row["period_end"], "2026-03-31")
        # 期末が新しい有報では置き換わる
        new_doc = dict(DOC, docID="S100NEW1", periodEnd="2027-03-31")
        fetch_edinet.save_document(
            self.conn, "7203", new_doc, build_zip(business_csv("<p>物流DX事業</p>")))
        row = self.conn.execute("SELECT * FROM business WHERE code='7203'").fetchone()
        self.assertEqual(row["description"], "物流DX事業")
        self.assertEqual(row["period_end"], "2027-03-31")

    def test_second_ingest_merges_by_year(self):
        text = (FIXTURES / "edinet-jpcrp-sample.csv").read_text(encoding="utf-8")
        fetch_edinet.save_document(self.conn, "7203", DOC, build_zip(text))
        doc2 = dict(DOC, docID="S100TES2", periodEnd="2026-03-31")
        fetch_edinet.save_document(self.conn, "7203", doc2, build_zip(MERGE_CSV))
        row = self.conn.execute(
            "SELECT * FROM financials WHERE code='7203' AND fiscal_year='2026/03'"
        ).fetchone()
        # 純利益は新しい値に、既存の売上はCOALESCEで維持される
        self.assertEqual(row["net_income"], 123456789)
        self.assertEqual(row["revenue"], 1500000000)


class TestIrbankHolderRefresh(unittest.TestCase):
    """IR BANKの大株主更新がEDINET由来の時点スナップショットを消さないこと。"""

    def test_edinet_only_as_of_survives(self):
        from src import fetch_irbank
        conn = get_conn(":memory:")
        # EDINETだけが持つ時点(IR BANKのページに無い日付)と、同時点の残留行
        conn.execute("INSERT INTO holders VALUES ('7203', '2027/09', 1, 'EDINET由来', 30.0)")
        conn.execute("INSERT INTO holders VALUES ('7203', '2026/03', 9, '残留ゴースト', 1.0)")
        html = (FIXTURES / "holder-7203.html").read_bytes()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(fetch_irbank, "DATA_DIR", Path(td)):
            fetch_irbank.save_company(conn, "7203", None, html)
        survived = {r["as_of"] for r in conn.execute(
            "SELECT DISTINCT as_of FROM holders WHERE code='7203'")}
        self.assertIn("2027/09", survived)  # EDINET時点は生き残る
        ghosts = conn.execute(
            "SELECT COUNT(*) FROM holders WHERE holder_name='残留ゴースト'").fetchone()[0]
        self.assertEqual(ghosts, 0)  # 再取得した時点内の残留行は消える


class TestDbMigration(unittest.TestCase):
    def test_old_db_gains_new_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.db"
            raw = sqlite3.connect(path)
            raw.execute("""CREATE TABLE financials (
                code TEXT NOT NULL, fiscal_year TEXT NOT NULL,
                is_forecast INTEGER NOT NULL DEFAULT 0, revenue INTEGER,
                PRIMARY KEY (code, fiscal_year))""")
            raw.execute("INSERT INTO financials(code, fiscal_year, revenue)"
                        " VALUES ('7203', '2026/03', 1)")
            raw.commit()
            raw.close()
            conn = get_conn(path)
            row = conn.execute("SELECT source, shares_issued FROM financials").fetchone()
            self.assertEqual(row["source"], "irbank")
            self.assertIsNone(row["shares_issued"])


if __name__ == "__main__":
    unittest.main()
