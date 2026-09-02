"""発見機③: 金利上昇局面で恩恵側になりやすい銘柄を「研究対象」として並べる
→ data/site/rates.html を生成。

判定機ではなく発見機(README参照)。「買え」ではなく、5つの分類ごとにDBに
ある指標で並べて人間が見るためのページ。分類は2026-08のユーザー整理:

  ① 銀行      地銀はメガより国内貸出比率が高く利ざや拡大の恩恵を受けやすい。
              再編の主導側/被統合側の区別はデータに無いので規模区分チップで補助
  ② 保険      運用利回りの改善が数年遅れて効く(銀行に遅行)
  ③ 証券・取引所  金利ある世界=個人が動く=手数料
  ④ ネットキャッシュ  現預金>総負債。受取利息がそのまま利益になる側
  ⑤ 不動産(選別)  借入負担で弱い側が多い。自己資本比率が高く、利上げ局面でも
              経常利益を伸ばした社だけを研究対象に

DBに無いもの(限界):
  - 貸出金・預金・国内貸出比率・有価証券の金利感応度 → 代わりに「利上げ局面
    (2024年度→直近)で実際に経常利益が伸びたか」を実測値として使う
    (政策金利は2024/03にマイナス解除、2026/08時点1.00%)
  - Jリート(銘柄マスタは内国株式のみ) → ホテル系リートは対象外
  - 有利子負債 → ネットキャッシュは「現預金−総負債」の厳しめ定義で近似
  - リース・消費者金融(その他金融業)は調達コスト上昇側なので対象外

使い方:
  .venv/bin/python -m src.rates              # 生成(分類ごと上位80)
  .venv/bin/python -m src.rates --top 40
  .venv/bin/python -m src.rates --check      # 参照銘柄がどこに載るか(キャリブレーション)
"""
import argparse
import sys
from typing import Dict, List, Optional, Tuple

from .db import get_conn
from .report import SITE_DIR, _shares, _yen, build_page_html, load_card_data
from .screen import (_consec_increases, load_all_metrics, ng_business_keyword,
                     percentile_map)

BUCKET_BANK = "① 銀行"
BUCKET_INSURANCE = "② 保険"
BUCKET_SECURITIES = "③ 証券・取引所"
BUCKET_NET_CASH = "④ ネットキャッシュ"
BUCKET_REAL_ESTATE = "⑤ 不動産(選別)"
BUCKETS = (BUCKET_BANK, BUCKET_INSURANCE, BUCKET_SECURITIES,
           BUCKET_NET_CASH, BUCKET_REAL_ESTATE)

# 利上げ局面の起点年度(2024/03のマイナス金利解除を含む2024年度)。
# この年度の経常利益 → 直近年度の経常利益 の伸びを「実測の金利感応度」とみなす
RATE_CYCLE_BASE_YEAR = "2024"
POLICY_RATE_NOTE = "2026/08時点 1.00%(2024/03 マイナス金利解除)"

# ---- スコアの重み(分類内パーセンタイル0〜1に掛ける。合計は自動で正規化) ----
# 銀行・保険・証券: 利上げ局面の実測増益率を主、一貫性(連続経常増益)とROEを従
FINANCIAL_WEIGHTS = {"rate_cycle_growth": 0.50, "consec_ord_up": 0.25, "roe": 0.25}
# ネットキャッシュ: 絶対額(任天堂型)・対時価総額(割安型)・対純利益(利益押上げ率)を等分
NET_CASH_WEIGHTS = {"net_cash": 1 / 3, "net_cash_to_mcap": 1 / 3, "rate_uplift": 1 / 3}
# 不動産: 借入負担が軽く(自己資本比率)、利上げ局面でも経常利益が伸びた社
REAL_ESTATE_WEIGHTS = {"equity_ratio": 0.50, "rate_cycle_growth": 0.50}
WEIGHTS_BY_BUCKET = {
    BUCKET_BANK: FINANCIAL_WEIGHTS, BUCKET_INSURANCE: FINANCIAL_WEIGHTS,
    BUCKET_SECURITIES: FINANCIAL_WEIGHTS, BUCKET_NET_CASH: NET_CASH_WEIGHTS,
    BUCKET_REAL_ESTATE: REAL_ESTATE_WEIGHTS,
}

# 参照銘柄: ユーザーが2026-08に挙げた「金利上昇で恩恵側」の例(推奨ではない)。
# 発見機②の教師データと同じ発想で「載るべき社が載っているか」を逆算チェックする
REFERENCE_CODES = {
    "8331": "千葉銀行", "5831": "しずおかFG", "8354": "ふくおかFG",
    "7186": "コンコルディアFG",
    "8750": "第一生命HD", "8795": "T&D HD", "8766": "東京海上HD", "8630": "SOMPO HD",
    "8697": "日本取引所グループ", "8604": "野村HD", "8601": "大和証券G",
    "7974": "任天堂", "6954": "ファナック", "4063": "信越化学",
}


def bucket_of(sector33: Optional[str], name: Optional[str]) -> Optional[str]:
    """JPX33業種(と社名)から分類を決める。Noneは対象外。"""
    s = sector33 or ""
    if "銀行" in s:
        return BUCKET_BANK
    if "保険" in s:
        return BUCKET_INSURANCE
    # 日本取引所グループは「その他金融業」なので社名で拾う
    if s.startswith("証券") or "取引所" in (name or ""):
        return BUCKET_SECURITIES
    if "不動産" in s:
        return BUCKET_REAL_ESTATE
    if "金融" in s:      # リース・消費者金融・クレジット: 調達コスト上昇側
        return None
    return BUCKET_NET_CASH  # 事業会社。net_cash>0 で別途絞る


def _latest(fin_rows: List[Dict], key: str):
    for r in reversed(fin_rows):
        if r.get(key) is not None:
            return r[key]
    return None


def rate_cycle_growth(fin_rows: List[Dict]) -> Optional[float]:
    """利上げ局面の実測増益率: 2024年度の経常利益 → 直近年度(それより後)の経常利益。
    どちらかが欠損・非正、または直近が2024年度以前ならNone(その指標では採点しない)。"""
    base = next((r["ordinary_income"] for r in fin_rows
                 if r["fiscal_year"].startswith(RATE_CYCLE_BASE_YEAR)
                 and r.get("ordinary_income") is not None), None)
    latest = next((r for r in reversed(fin_rows)
                   if r.get("ordinary_income") is not None), None)
    if base is None or latest is None or latest["fiscal_year"][:4] <= RATE_CYCLE_BASE_YEAR:
        return None
    if base <= 0 or latest["ordinary_income"] <= 0:
        return None
    return latest["ordinary_income"] / base - 1.0


def financial_metrics(fin_rows: List[Dict]) -> Dict:
    """全分類共通の金利関連指標(経常利益ベース。金利コスト/収益を含んだ後の利益)。"""
    return {
        "ordinary_income": _latest(fin_rows, "ordinary_income"),
        "consec_ord_up": _consec_increases([r.get("ordinary_income") for r in fin_rows]),
        "roe": _latest(fin_rows, "roe"),
        "rate_cycle_growth": rate_cycle_growth(fin_rows),
    }


def net_cash_metrics(fin_rows: List[Dict]) -> Dict:
    """ネットキャッシュ = 現預金 − 総負債(総資産−純資産)。3値が揃う直近年度で計算。
    有利子負債はDBに無いので「全負債を現預金で賄える」厳しめの定義。
    rate_uplift = ネットキャッシュ×1% ÷ 純利益(金利+1%が利益を何%押し上げるかの概算)。"""
    for r in reversed(fin_rows):
        cash, ta, na = r.get("cash"), r.get("total_assets"), r.get("net_assets")
        if cash is None or ta is None or na is None:
            continue
        net_cash = cash - (ta - na)
        ni = r.get("net_income")
        return {
            "cash": cash, "net_cash": net_cash, "net_cash_fy": r["fiscal_year"],
            "rate_uplift": (net_cash * 0.01 / ni
                            if ni and ni > 0 and net_cash > 0 else None),
        }
    return {}


def _chips(bucket: str, m: Dict) -> List[Tuple[str, str]]:
    pbr, roe, g = m.get("pbr"), m.get("roe"), m.get("rate_cycle_growth")
    chips = []
    if m.get("scale") and m["scale"] != "-":   # JPXマスタはTOPIX外を"-"で出す
        chips.append(("規模", m["scale"]))
    chips += [
        ("PBR", f"{pbr:.2f}倍" if pbr else "-"),
        ("ROE", f"{roe:.1f}%" if roe is not None else "-"),
        ("経常利益", _yen(m.get("ordinary_income"))),
        (f"経常利益 {RATE_CYCLE_BASE_YEAR}年度→直近", f"{g * 100:+.0f}%" if g is not None else "-"),
        ("連続経常増益", f"{m.get('consec_ord_up', 0)}期"),
    ]
    if bucket == BUCKET_NET_CASH:
        r, u = m.get("net_cash_to_mcap"), m.get("rate_uplift")
        chips += [
            (f"ネットキャッシュ({m.get('net_cash_fy', '-')})", _yen(m.get("net_cash"))),
            ("対時価総額", f"{r * 100:.0f}%" if r is not None else "-"),
            ("金利+1%の利益押上げ", f"{u * 100:+.1f}%" if u is not None else "-"),
        ]
    return chips


def build_rate_rows(conn, top: int) -> List[Dict]:
    """分類ごとに分類内パーセンタイルで採点し、分類順に並べた行を返す(rankは分類内)。"""
    base = load_all_metrics(conn)  # 発見機①と同じ基礎指標(大株主・CAGR等)。3期未満はここで落ちる
    fins: Dict[str, List[Dict]] = {}
    for r in conn.execute(
            """SELECT f.* FROM financials f JOIN companies c ON c.code = f.code
               WHERE f.is_forecast = 0 ORDER BY f.code, f.fiscal_year"""):
        fins.setdefault(r["code"], []).append(dict(r))
    companies = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, market, sector33, scale FROM companies")}
    prices = {r["code"]: (r["close"], r["mktcap"]) for r in
              conn.execute("SELECT code, close, mktcap FROM prices")}
    texts = {r["code"]: r["description"] for r in
             conn.execute("SELECT code, description FROM business")}

    per_bucket: Dict[str, List[Tuple[str, Dict]]] = {b: [] for b in BUCKETS}
    for code, m in base.items():
        c, rows = companies.get(code), fins.get(code)
        if not c or not rows or ng_business_keyword(texts.get(code)):
            continue
        bucket = bucket_of(c["sector33"], c["name"])
        if bucket is None:
            continue
        m = dict(m, themes=[bucket], scale=c["scale"])
        m.update(financial_metrics(rows))
        close, mcap = prices.get(code, (None, None))
        if mcap is None and close:   # J-Quantsの公式値が無ければ終値×推定株式数
            shares = _shares(rows)
            mcap = close * shares if shares else None
        na = _latest(rows, "net_assets")
        m["mktcap"] = mcap
        m["pbr"] = mcap / na if mcap and na and na > 0 else None
        if bucket == BUCKET_NET_CASH:
            m.update(net_cash_metrics(rows))
            if not m.get("net_cash") or m["net_cash"] <= 0:
                continue
            m["net_cash_to_mcap"] = m["net_cash"] / mcap if mcap else None
        m["extra_chips"] = _chips(bucket, m)
        per_bucket[bucket].append((code, m))

    out = []
    for bucket in BUCKETS:
        items = per_bucket[bucket]
        weights = WEIGHTS_BY_BUCKET[bucket]
        # パーセンタイルは分類内の全社を母集団に計算(上位で切る前)
        pct = {k: percentile_map({code: m.get(k) for code, m in items}) for k in weights}
        scored = []
        for code, m in items:
            num = den = 0.0
            for k, w in weights.items():
                if code in pct[k]:
                    num += w * pct[k][code]
                    den += w
            if den > 0:
                scored.append((num / den, code, m))
        scored.sort(reverse=True)
        for rank, (score, code, m) in enumerate(scored[:top], 1):
            c = companies[code]
            out.append({"rank": rank, "code": code, "score": score,
                        "name": c["name"], "market": c["market"],
                        "sector33": c["sector33"], "metrics": m})
    return out


def reference_ranks(rows: List[Dict]) -> List[str]:
    """参照銘柄が分類内の何位に載ったか(載らなければ理由の当たり)を1行ずつ返す。"""
    by_code = {r["code"]: r for r in rows}
    lines = []
    for code, name in REFERENCE_CODES.items():
        r = by_code.get(code)
        if r:
            lines.append(f"{code} {name}: {r['metrics']['themes'][0]} #{r['rank']} "
                         f"(score {r['score']:.3f})")
        else:
            lines.append(f"{code} {name}: 圏外(表示上限・財務3期未満・ネットキャッシュ≤0のいずれか)")
    return lines


def generate(conn, top: int):
    rows = build_rate_rows(conn, top)
    if not rows:
        print("対象がありません(companies/financialsが空の可能性)")
        return None
    data = load_card_data(conn, [r["code"] for r in rows])
    counts: Dict[str, int] = {}
    for r in rows:
        b = r["metrics"]["themes"][0]
        counts[b] = counts.get(b, 0) + 1
    logic_body = (
        f'金利上昇サイクルの典型的な波及順は「地銀→保険→証券」。分類ごとに'
        f'<b>分類内パーセンタイル</b>で採点し、分類内の順位で並べる(分類をまたぐ比較はしない)。'
        f'① 銀行・② 保険・③ 証券・取引所: 経常利益 {RATE_CYCLE_BASE_YEAR}年度→直近の実測増益率 50% / '
        f'連続経常増益 25% / ROE 25%。貸出比率や再編の主導/被統合はDBに無いので、'
        f'規模区分(Core30・Large70=大手)とPBRを表示して人間が補う。'
        f'PBR0.1倍級の超割安は「安い=良い」ではなく貸出先の信用リスクを要チェック。'
        f'④ ネットキャッシュ: 現預金−総負債&gt;0 の事業会社(金融除く)。絶対額 / 対時価総額 / '
        f'金利+1%の利益押上げ(ネットキャッシュ×1%÷純利益)を等分。'
        f'⑤ 不動産(選別): 自己資本比率 50% / 実測増益率 50%。多くは借入負担で弱い側なので上位だけ見る。'
        f'Jリート(ホテル系)は銘柄マスタ外で対象外。政策金利 {POLICY_RATE_NOTE}。'
        f'推奨ではなく研究対象の一覧。')
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "rates.html"
    out.write_text(build_page_html(
        rows, data,
        title="kabu-screener 発見機③ 金利上昇",
        heading='発見機③ <em>金利上昇</em>',
        lead=("金利が上がった時に恩恵側になりやすい5分類(銀行・保険・証券・"
              "ネットキャッシュ・不動産選別)を、利上げ局面の実測増益率などで分類内順位に並べる"),
        pills=[f"対象 <b>{len(rows)}</b>社"]
              + [f"{b} <b>{n}</b>" for b, n in counts.items()]
              + ["分類内ランキング", "推奨ではなく研究対象"],
        tab_key="themes",
        logic_summary="発見機③のロジック",
        logic_body=logic_body,
        other_page=[("index.html", "発見機① 財務"),
                    ("discover.html", "発見機② テーマ×事業モデル")],
        tab_order=BUCKETS,
    ), encoding="utf-8")
    print(f"{out} を生成しました({len(rows)}行: "
          + " / ".join(f"{b} {n}" for b, n in counts.items()) + ")")
    print("参照銘柄の位置:\n  " + "\n  ".join(reference_ranks(rows)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=80, help="分類ごとの表示上限")
    ap.add_argument("--check", action="store_true",
                    help="ページを生成せず、参照銘柄が分類内の何位かだけ表示")
    args = ap.parse_args()
    conn = get_conn()
    if args.check:
        print("\n".join(reference_ranks(build_rate_rows(conn, 10 ** 6))))
        return
    if generate(conn, args.top) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
