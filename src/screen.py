"""スクリーニング: 指標計算 → ハードフィルタ → パーセンタイルスコア → ランキング。

設計方針(会話で決めたこと):
- このツールは「判定機」ではなく「発見機」。ハードフィルタは最小限にして、
  指標はパーセンタイル(全社の中での相対位置)でスコア化し、上から人間が見る。
- 閾値・重みはこのファイル冒頭の定数を書き換えて試行錯誤する。
- --stats で各指標の分布を出せる(「ちゃんと増収」のラインを実データから決める用)。

使い方:
  .venv/bin/python -m src.screen            # スクリーニング実行、上位を表示+DB保存
  .venv/bin/python -m src.screen --top 50
  .venv/bin/python -m src.screen --stats    # 指標の分布(キャリブレーション用)
"""
import argparse
import json
import re
from datetime import date
from typing import Dict, List, Optional

from .db import get_conn

# ---- ハードフィルタ(最小限。ここを緩めて/締めて試行錯誤する) ----
# オーナー保有率とVC不在はマスト条件にしない(オーナーは「できれば」なので
# WEIGHTSのowner_ratioで加点のみ、VCは除外せず一覧・通知に表示して人間が判断)
MIN_OWNER_RATIO = 0.0    # 個人大株主の合計保有率(%)がこれ以上。0.0=無効
EXCLUDE_VC = False       # 直近の大株主にVC系がいたら除外
REQUIRE_OP_CF_NONNEG = True  # 収録期間内に営業CFの赤字なし

# 「明らかに微妙な事業」の除外ワード(businessテーブルの事業内容全文への部分一致)。
# 有報の長文に当てるので固有性の高い語だけ入れること(「金融」「不動産」の
# ような広い語は本業でない銘柄まで巻き込む)。事業内容が未取得の銘柄は除外しない
NG_BUSINESS_KEYWORDS = [
    "パチンコ", "遊技機", "消費者金融",
]

# ---- スコアの重み(パーセンタイル0〜1に掛ける。合計は自動で正規化) ----
# 営業CFマージンと非希薄化は2026-08-28にスコアから除外(ユーザー判断)。
# CFの健全性はハードフィルタ(赤字なし)で担保し、希薄化は表示のみ
WEIGHTS = {
    "rev_cagr": 0.40,        # 売上CAGR(「ちゃんと」成長の水準)
    "consec_growth": 0.25,   # 連続増収増益の年数(一貫性)
    "owner_ratio": 0.20,     # オーナー個人の保有率
    "equity_ratio": 0.15,    # 自己資本比率
}

# ---- 大株主の分類ルール ----
# 法人・機関っぽい名前のマーカー(これに当たらなければ個人とみなす)
CORPORATE_MARKERS = [
    "株式会社", "(株)", "（株）", "有限会社", "合同会社", "合資会社",
    "銀行", "信託", "生命", "海上", "火災", "保険", "証券", "金庫", "農中",
    "組合", "共済", "ホールディングス", "グループ", "ファンド", "機構",
    "持株会", "持株制度", "財団", "学園", "大学", "会館", "共栄会", "協力会",
    "興産", "商事", "商店", "工業", "産業", "電機", "電力", "鉄道", "不動産",
    "カンパニー", "バンク", "トラスト", "パートナーズ", "キャピタル",
    "インベストメント", "アセット", "マネジメント", "マネージメント",
    "ファイナンス", "ファイナンシャル", "リミテッド", "エルピー",
    "インターナショナル", "ノミニーズ", "クリアストリーム", "ユーロクリア",
    "ステートストリート", "ノーザン", "モルガン", "ゴールドマン", "メロン",
    "バンカース", "ドイチェ", "ソシエテ", "エイチエスビーシー", "シティ",
    "スカンディナヴィスカ", "セコム", "興業", "商会", "企画", "事務所",
    "BANK", "TRUST", "LTD", "LIMITED", "LLC", "INC", "L.P.", "LP",
    "N.V.", "S.A.", "PLC", "CO.", "FUND", "NOMINEES", "PARTNERS",
    "CAPITAL", "INVESTMENT", "MANAGEMENT", "SECURITIES", "GK", "合同",
]
# VC・投資ファンドっぽいマーカー(法人のうちさらにこれに当たるもの)
VC_MARKERS = [
    "投資事業有限責任組合", "投資事業組合", "ベンチャー", "キャピタル",
    "インキュベイト", "ジャフコ", "グロービス", "グローバル・ブレイン",
    "インベストメント・エルピー", "SBIインベストメント", "フューチャーベンチャー",
]
# 信託口など「実質株主が見えない」名義(オーナー比率の計算から単に除外)
CUSTODIAN_MARKERS = [
    "マスタートラスト", "カストディ", "信託口", "証券保管振替機構",
    "日本トラスティ", "資産管理サービス", "SSBTC", "DFA", "RE FUND",
]

_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def classify_holder(name: str) -> str:
    """'individual' / 'vc' / 'custodian' / 'corporate' のどれかを返す。"""
    upper = name.upper()
    for m in CUSTODIAN_MARKERS:
        if m.upper() in upper:
            return "custodian"
    for m in VC_MARKERS:
        if m.upper() in upper:
            return "vc"
    for m in CORPORATE_MARKERS:
        if m.upper() in upper:
            return "corporate"
    # 英数字が多い名前は海外機関の可能性が高いので法人扱いに倒す
    if len(_ALNUM_RE.findall(name)) >= max(3, len(name) // 2):
        return "corporate"
    return "individual"


def _consec_increases(vals: List[Optional[float]]) -> int:
    """末尾(最新)から遡って前年比プラスが続いた回数。"""
    n = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] is not None and vals[i - 1] is not None and vals[i] > vals[i - 1]:
            n += 1
        else:
            break
    return n


def _cagr(years: List[str], vals: List[Optional[float]]) -> Optional[float]:
    pairs = [(y, v) for y, v in zip(years, vals) if v is not None and v > 0]
    if len(pairs) < 2:
        return None
    (fy0, v0), (fy1, v1) = pairs[0], pairs[-1]
    span = int(fy1[:4]) - int(fy0[:4])
    if span <= 0:
        return None
    return (v1 / v0) ** (1.0 / span) - 1.0


def compute_metrics(fin_rows: List[Dict], holder_rows: List[Dict]) -> Optional[Dict]:
    """1社分の指標を計算。データ不足ならNone。"""
    actual = [r for r in fin_rows if not r["is_forecast"]]
    if len(actual) < 3:
        return None
    years = [r["fiscal_year"] for r in actual]
    rev = [r["revenue"] for r in actual]
    op = [r["op_income"] for r in actual]

    # 希薄化: 株式数 ≒ (株主資本 or 純資産)/BPS の変化率で近似。
    # 1株当たり指標(BPS)は株式分割時に遡及修正されることが多く、発行済株式総数の
    # 生値と違って分割を希薄化と誤認しにくい(shares_issuedは保存のみで未使用)。
    # ただし有報サマリーは分割を「実施期の翌期首起点」で仮定計算するため、
    # 最古年度(4期前)は未修正のことがある(9983の2021/08で実測)。IR BANK由来の
    # 古い年度との境界も同様(既知の制約。分割イベントはJ-Quants導入時に扱う)
    shares = []
    for r in actual:
        base = r.get("shareholders_equity") or r.get("net_assets")
        bps = r.get("bps")
        shares.append(base / bps if base and bps else None)
    share_pairs = [s for s in shares if s]
    dilution = (share_pairs[-1] / share_pairs[0] - 1.0) if len(share_pairs) >= 2 else None

    cf_vals = [r["op_cf"] for r in actual if r["op_cf"] is not None]
    cf_margins = [r["op_cf_margin"] for r in actual if r["op_cf_margin"] is not None]

    # 大株主: 最新時点のスナップショットで分類
    latest_as_of = max((h["as_of"] for h in holder_rows), default=None)
    latest_holders = [h for h in holder_rows if h["as_of"] == latest_as_of]
    individuals = [h for h in latest_holders if classify_holder(h["holder_name"]) == "individual"]
    vcs = [h for h in latest_holders if classify_holder(h["holder_name"]) == "vc"]

    return {
        "years": len(actual),
        "latest_fy": years[-1],
        "rev_cagr": _cagr(years, rev),
        "consec_rev_up": _consec_increases(rev),
        "consec_op_up": _consec_increases(op),
        "consec_growth": min(_consec_increases(rev), _consec_increases(op)),
        "op_cf_margin": sum(cf_margins[-3:]) / len(cf_margins[-3:]) if cf_margins else None,
        "op_cf_all_nonneg": bool(cf_vals) and all(v >= 0 for v in cf_vals),
        "equity_ratio": next((r["equity_ratio"] for r in reversed(actual)
                              if r["equity_ratio"] is not None), None),
        "dilution": dilution,
        "owner_ratio": round(sum(h["ratio"] or 0 for h in individuals), 2),
        "owner_names": [f"{h['holder_name']}({h['ratio']}%)" for h in individuals[:3]],
        "has_vc": bool(vcs),
        "vc_names": [h["holder_name"] for h in vcs],
        "holder_as_of": latest_as_of,
    }


def passes_hard_filter(m: Dict) -> bool:
    if m["owner_ratio"] < MIN_OWNER_RATIO:
        return False
    if EXCLUDE_VC and m["has_vc"]:
        return False
    if REQUIRE_OP_CF_NONNEG and not m["op_cf_all_nonneg"]:
        return False
    return True


def ng_business_keyword(description: Optional[str]) -> Optional[str]:
    """事業内容がNGワードに当たれば最初のキーワードを返す(未取得はNoneで通す)。"""
    if not description:
        return None
    return next((kw for kw in NG_BUSINESS_KEYWORDS if kw in description), None)


def _snippet(text: Optional[str], limit: int = 120) -> Optional[str]:
    """事業内容の冒頭を1行に潰して返す(一覧・通知カード表示用)。"""
    if not text:
        return None
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def percentile_map(values: Dict[str, Optional[float]]) -> Dict[str, float]:
    """code->値 を code->パーセンタイル(0〜1) に変換。Noneは含めない。

    同値には同じパーセンタイル(順位の平均)を与える。連続増収増益のような
    整数指標で、コード順によってスコアが変わるのを防ぐ。
    """
    items = sorted(((v, c) for c, v in values.items() if v is not None),
                   key=lambda t: t[0])
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][1]: 0.5}
    out: Dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        pct = (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            out[items[k][1]] = pct
        i = j + 1
    return out


def load_all_metrics(conn) -> Dict[str, Dict]:
    # 母集団は現在の上場銘柄のみ(上場廃止銘柄の残留データを除外)
    fins: Dict[str, List[Dict]] = {}
    for r in conn.execute(
        """SELECT f.* FROM financials f
           JOIN companies c ON c.code = f.code
           ORDER BY f.code, f.fiscal_year"""):
        fins.setdefault(r["code"], []).append(dict(r))
    holds: Dict[str, List[Dict]] = {}
    for r in conn.execute("SELECT * FROM holders"):
        holds.setdefault(r["code"], []).append(dict(r))

    metrics = {}
    for code, fin_rows in fins.items():
        m = compute_metrics(fin_rows, holds.get(code, []))
        if m:
            metrics[code] = m
    return metrics


def run_screen(conn, top: int):
    metrics = load_all_metrics(conn)
    if not metrics:
        print("データがありません。先に fetch_jpx / fetch_irbank を実行してください")
        return

    names = {r["code"]: (r["name"], r["market"]) for r in
             conn.execute("SELECT code, name, market FROM companies")}

    # パーセンタイルは「データのある全社」を母集団に計算(フィルタ後だと分布が歪む)
    pct = {}
    pct["rev_cagr"] = percentile_map({c: m["rev_cagr"] for c, m in metrics.items()})
    pct["consec_growth"] = percentile_map({c: float(m["consec_growth"]) for c, m in metrics.items()})
    pct["owner_ratio"] = percentile_map({c: m["owner_ratio"] for c, m in metrics.items()})
    pct["equity_ratio"] = percentile_map({c: m["equity_ratio"] for c, m in metrics.items()})

    # 事業内容(EDINET由来。未取り込みなら空でNG除外は発動しない)
    businesses = {r["code"]: r["description"] for r in
                  conn.execute("SELECT code, description FROM business")}

    candidates = {}
    ng_count = 0
    for c, m in metrics.items():
        if not passes_hard_filter(m):
            continue
        if ng_business_keyword(businesses.get(c)):
            ng_count += 1
            continue
        m["business"] = _snippet(businesses.get(c))
        candidates[c] = m
    scored = []
    for code, m in candidates.items():
        num = den = 0.0
        for key, w in WEIGHTS.items():
            if code in pct[key]:
                num += w * pct[key][code]
                den += w
        if den == 0:
            continue
        scored.append((num / den, code, m))
    scored.sort(reverse=True)

    run_date = date.today().isoformat()
    with conn:
        conn.execute("DELETE FROM screen_results WHERE run_date = ?", (run_date,))
        for rank, (score, code, m) in enumerate(scored, 1):
            conn.execute(
                """INSERT INTO screen_results(run_date, code, rank, score, metrics_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_date, code, rank, round(score, 4), json.dumps(m, ensure_ascii=False)),
            )

    conds = []
    if MIN_OWNER_RATIO > 0:
        conds.append(f"オーナー{MIN_OWNER_RATIO:g}%+")
    if EXCLUDE_VC:
        conds.append("VCなし")
    if REQUIRE_OP_CF_NONNEG:
        conds.append("営業CF赤字なし")
    ng_note = f" / 事業内容NG {ng_count}社除外" if ng_count else ""
    print(f"対象 {len(metrics)}社 → ハードフィルタ通過 {len(scored)}社 "
          f"({' / '.join(conds) or 'ハードフィルタなし'}{ng_note})\n")
    fmt = "{:<6} {:<20} {:>6} {:>7} {:>5} {:>7} {:>6} {:>7}  {}"
    print(fmt.format("code", "銘柄", "score", "CAGR", "連続", "CF率", "自己資", "希薄化", "オーナー"))
    for score, code, m in scored[:top]:
        name, market = names.get(code, ("?", "?"))
        print(fmt.format(
            code, name[:10], f"{score:.3f}",
            f"{m['rev_cagr'] * 100:.1f}%" if m["rev_cagr"] is not None else "-",
            f"{m['consec_growth']}期",
            f"{m['op_cf_margin']:.1f}%" if m["op_cf_margin"] is not None else "-",
            f"{m['equity_ratio']:.0f}%" if m["equity_ratio"] is not None else "-",
            f"{m['dilution'] * 100:+.1f}%" if m["dilution"] is not None else "-",
            (" ".join(m["owner_names"]) or "-")
            + (f" [VC: {' '.join(m['vc_names'][:2])}]" if m["has_vc"] else ""),
        ))
        if m.get("business"):
            print(f"       └ {m['business'][:60]}")
    print(f"\n結果をscreen_results({run_date})に保存しました。通知: python -m src.notify")


def show_stats(conn):
    """指標の分布を出す。「ちゃんと増収」のラインを実データから決めるためのもの。"""
    metrics = load_all_metrics(conn)
    print(f"データのある{len(metrics)}社の分布(10/25/50/75/90パーセンタイル):\n")
    for key, label, scale in [
        ("rev_cagr", "売上CAGR", 100), ("consec_growth", "連続増収増益(期)", 1),
        ("op_cf_margin", "営業CFマージン%", 1), ("equity_ratio", "自己資本比率%", 1),
        ("dilution", "株式数変化率%", 100), ("owner_ratio", "個人大株主合計%", 1),
    ]:
        vals = sorted(m[key] * scale for m in metrics.values() if m[key] is not None)
        if not vals:
            print(f"{label:>16}: データなし")
            continue
        qs = [vals[min(len(vals) - 1, int(len(vals) * q))] for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
        print(f"{label:>16}: " + "  ".join(f"{q:8.1f}" for q in qs) + f"   (n={len(vals)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--stats", action="store_true", help="指標の分布を表示(キャリブレーション用)")
    args = ap.parse_args()
    conn = get_conn()
    if args.stats:
        show_stats(conn)
    else:
        run_screen(conn, args.top)


if __name__ == "__main__":
    main()
