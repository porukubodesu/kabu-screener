"""発見機②: テーマ×事業モデルのスクリーニング → data/site/discover.html を生成。

発見機①(screen.py)との違い:
- 営業CFのハードフィルタなし(パワーエックスのような先行投資期の赤字を許容)
- 事業内容テキストから受託・コンサル・不動産系モデルを除外(themes.py)
- テーマ(人口減・エネルギー・リユース等)に1つ以上当たる銘柄のみ
- スコアは成長寄り: 売上CAGR 50% / 連続増収増益 25% / オーナー保有率 25%
- テーマ別タブで表示(1銘柄が複数テーマに載ることもある)

使い方:
  .venv/bin/python -m src.discover            # 最新DBから生成
  .venv/bin/python -m src.discover --top 400
"""
import argparse
import sys

from .db import get_conn
from .report import SITE_DIR, build_page_html, load_card_data
from .screen import load_all_metrics, ng_business_keyword, percentile_map
from .themes import MODEL_OTHER, MODEL_OWN, classify_model, match_themes

DISCOVER_WEIGHTS = {"rev_cagr": 0.50, "consec_growth": 0.25, "owner_ratio": 0.25}


def build_discover_rows(conn, top: int):
    """テーマ発見機のランキング行(report.pyのカード描画に渡す形)を作る。"""
    metrics = load_all_metrics(conn)
    texts = {r["code"]: r["description"] for r in
             conn.execute("SELECT code, description FROM business")}
    names = {r["code"]: r for r in
             conn.execute("SELECT code, name, market, sector33 FROM companies")}
    pct = {
        "rev_cagr": percentile_map({c: m["rev_cagr"] for c, m in metrics.items()}),
        "consec_growth": percentile_map(
            {c: float(m["consec_growth"]) for c, m in metrics.items()}),
        "owner_ratio": percentile_map(
            {c: m["owner_ratio"] for c, m in metrics.items()}),
    }

    scored = []
    for code, m in metrics.items():
        text = texts.get(code)
        if not text or code not in names:
            continue
        if ng_business_keyword(text):
            continue
        if classify_model(text) not in (MODEL_OWN, MODEL_OTHER):
            continue
        themes = match_themes(text)
        if not themes:
            continue
        num = den = 0.0
        for key, w in DISCOVER_WEIGHTS.items():
            if code in pct[key]:
                num += w * pct[key][code]
                den += w
        if den == 0:
            continue
        m = dict(m, themes=themes)
        scored.append((num / den, code, m))
    scored.sort(reverse=True)

    return [
        {"rank": rank, "code": code, "score": score,
         "name": names[code]["name"], "market": names[code]["market"],
         "sector33": names[code]["sector33"], "metrics": m}
        for rank, (score, code, m) in enumerate(scored[:top], 1)]


def generate(conn, top: int):
    rows = build_discover_rows(conn, top)
    if not rows:
        print("対象がありません(businessテーブルが空の可能性)")
        return None
    data = load_card_data(conn, [r["code"] for r in rows])
    weights = "売上CAGR 50% / 連続増収増益 25% / オーナー保有率 25%"
    logic_body = (
        f'① 有報「事業の内容」の全文から、受託・コンサル・人材派遣・不動産系の'
        f'モデルを除外し、テーマ辞書(src/themes.py)に当たる銘柄だけを残す。'
        f'判定は保守的(明確な場合のみ除外)で、見逃しはあり得る。'
        f'② 営業CFのハードフィルタは<b>掛けない</b> — 先行投資期の赤字を許容する。'
        f'③ スコアは全上場企業内パーセンタイルの加重平均 — {weights}。')
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "discover.html"
    out.write_text(build_page_html(
        rows, data,
        title="kabu-screener 発見機② テーマ",
        heading='発見機② <em>テーマ×事業モデル</em>',
        lead=("受託・コンサル・不動産系を除いた「自社プロダクトを作って売る」会社を、"
              "テーマ(省人化・エネルギー・リユース…)別に成長順で見る"),
        pills=[f"対象 <b>{len(rows)}</b>社",
               "CF赤字許容", "受託・不動産除外"],
        tab_key="themes",
        logic_summary="発見機②のロジック",
        logic_body=logic_body,
        other_page=("index.html", "発見機① 財務"),
    ), encoding="utf-8")
    print(f"{out} を生成しました({len(rows)}行)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400)
    args = ap.parse_args()
    if generate(get_conn(), args.top) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
