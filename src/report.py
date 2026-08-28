"""スクリーニング結果の静的HTMLレポートを生成する。

data/site/index.html に自己完結のページ(外部アセットなし)を書き出す。
daily.sh がこれを site ブランチに載せてGitHub Pagesで公開する。

使い方:
  .venv/bin/python -m src.report            # 最新のscreen_resultsから生成
  .venv/bin/python -m src.report --top 50
"""
import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .db import DATA_DIR, get_conn
from .screen import (EXCLUDE_VC, MIN_OWNER_RATIO, NG_BUSINESS_KEYWORDS,
                     REQUIRE_OP_CF_NONNEG, WEIGHTS)

SITE_DIR = DATA_DIR / "site"

WEIGHT_LABELS = {
    "rev_cagr": "売上CAGR", "consec_growth": "連続増収増益",
    "op_cf_margin": "営業CFマージン", "owner_ratio": "オーナー保有率",
    "equity_ratio": "自己資本比率", "anti_dilution": "非希薄化",
}

# 配色はdatavizスキルの検証済みリファレンスパレット(役割トークンのみ使用)
CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --bar: #2a78d6; --bar-track: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --bar: #3987e5; --bar-track: #383835;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.6;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 16px 48px; }
header h1 { font-size: 20px; margin: 0; }
header .sub { color: var(--ink-2); margin: 4px 0 0; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; margin: 20px 0; }
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 16px; min-width: 130px;
}
.tile .v { font-size: 22px; font-weight: 600; }
.tile .k { color: var(--muted); font-size: 12px; }
.hero {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 20px; margin: 0 0 24px;
}
.hero .rank { color: var(--muted); font-size: 12px; }
.hero .name { font-size: 18px; font-weight: 600; }
.hero .name a { color: inherit; }
.hero .metrics { color: var(--ink-2); margin-top: 4px; }
.hero .biz { color: var(--ink-2); margin-top: 8px; font-size: 13px; }
.hero .owners { color: var(--muted); margin-top: 4px; font-size: 13px; }
.logic {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 20px; margin: 0 0 16px;
  font-size: 13px; color: var(--ink-2);
}
.logic b { color: var(--ink); }
.tablebox {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow-x: auto;
}
table { border-collapse: collapse; width: 100%; min-width: 860px; }
details.fin { margin-top: 4px; }
details.fin summary { cursor: pointer; color: var(--muted); font-size: 12px; }
.fin table { width: auto; min-width: 0; margin: 6px 0 2px; }
.fin th { position: static; background: none; border-bottom: 1px solid var(--grid); padding: 2px 14px 2px 0; }
.fin td { color: var(--ink-2); font-size: 12px; border-bottom: 0; padding: 2px 14px 2px 0; }
th {
  text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
  padding: 10px 8px; border-bottom: 1px solid var(--grid);
  position: sticky; top: 0; background: var(--surface);
}
td { padding: 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tbody tr:hover { background: color-mix(in srgb, var(--ink) 4%, transparent); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.code a { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--muted); }
.co { font-weight: 600; }
.sector { color: var(--muted); font-size: 12px; }
.scorebar {
  display: inline-block; width: 72px; height: 6px; background: var(--bar-track);
  border-radius: 0 4px 4px 0; vertical-align: 2px; margin-left: 8px;
}
.scorebar i { display: block; height: 100%; background: var(--bar);
  border-radius: 0 4px 4px 0; }
.biz { color: var(--muted); font-size: 12px; max-width: 420px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.owners { font-size: 12px; color: var(--ink-2); }
.chip {
  display: inline-block; border: 1px solid var(--border); border-radius: 4px;
  color: var(--muted); font-size: 11px; padding: 0 4px; margin-left: 4px;
}
.warn { color: var(--muted); font-size: 11px; }
footer { color: var(--muted); font-size: 12px; margin-top: 24px; }
"""


def _esc(s: Optional[str]) -> str:
    return html.escape(s or "", quote=True)


def _pct(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def _yen(v: Optional[float]) -> str:
    """円の生値を兆/億で表示。"""
    if v is None:
        return "-"
    if abs(v) >= 1e12:
        return f"{v / 1e12:,.2f}兆"
    return f"{v / 1e8:,.0f}億"


def _fin_details_html(fin_rows) -> str:
    """決算推移(実績+予想)の折りたたみ表。"""
    if not fin_rows:
        return ""
    body = "\n".join(
        f"<tr><td>{_esc(f['fiscal_year'])}{'(予)' if f['is_forecast'] else ''}</td>"
        f"<td class=\"num\">{_yen(f['revenue'])}</td>"
        f"<td class=\"num\">{_yen(f['op_income'])}</td>"
        f"<td class=\"num\">{_yen(f['net_income'])}</td>"
        f"<td class=\"num\">{_yen(f['op_cf'])}</td></tr>"
        for f in fin_rows)
    return (f'<details class="fin"><summary>決算推移</summary><table>'
            f'<tr><th>年度</th><th class="num">売上</th><th class="num">営利</th>'
            f'<th class="num">純利</th><th class="num">営業CF</th></tr>'
            f'{body}</table></details>')


def _logic_html() -> str:
    conds = []
    if MIN_OWNER_RATIO > 0:
        conds.append(f"オーナー保有率{MIN_OWNER_RATIO:g}%以上")
    if EXCLUDE_VC:
        conds.append("VC不在")
    if REQUIRE_OP_CF_NONNEG:
        conds.append("収録期間に営業CFの赤字なし")
    weights = " / ".join(f"{WEIGHT_LABELS.get(k, k)} {w * 100:.0f}%"
                         for k, w in WEIGHTS.items())
    ng = "・".join(NG_BUSINESS_KEYWORDS)
    return f"""<div class="logic">
<b>ロジック</b>: ① 候補条件は「{"・".join(conds) or "なし"}」と事業内容のNGワード({_esc(ng)})除外のみ。
② スコアは各指標の<b>全上場企業内パーセンタイル</b>(0〜1)の加重平均 — {weights}。
オーナー保有・VC不在は必須条件にせず、スコアと表示で判断材料にする。
③ 判定機ではなく<b>発見機</b> — 上位から人間が四季報などで定性確認する前提。閾値の発明はしない。
</div>"""


def _row_html(rank: int, code: str, name: str, sector: str, score: float,
              m: Dict, fin_rows=()) -> str:
    dilution = m.get("dilution")
    # 分割イベントの境界誤差の可能性が高い値には注記を付ける(既知の制約)
    dilution_note = (' <span class="warn" title="株式分割の境界誤差の可能性">⚠分割?</span>'
                     if dilution is not None and abs(dilution) > 0.5 else "")
    owners = " ".join(_esc(o) for o in (m.get("owner_names") or [])) or "-"
    vc = (f'<span class="chip" title="{_esc(" / ".join(m.get("vc_names") or []))}">VC</span>'
          if m.get("has_vc") else "")
    biz = f'<div class="biz">{_esc(m.get("business"))}</div>' if m.get("business") else ""
    fin = _fin_details_html(fin_rows)
    return f"""<tr>
<td class="num">{rank}</td>
<td class="code"><a href="https://irbank.net/{_esc(code)}" target="_blank" rel="noopener">{_esc(code)}</a></td>
<td><span class="co">{_esc(name)}</span> <span class="sector">{_esc(sector)}</span>{biz}{fin}</td>
<td class="num">{score:.3f}<span class="scorebar"><i style="width:{score * 100:.0f}%"></i></span></td>
<td class="num">{_pct(m.get("rev_cagr"))}</td>
<td class="num">{m.get("consec_growth", "-")}期</td>
<td class="num">{f"{m['op_cf_margin']:.1f}%" if m.get("op_cf_margin") is not None else "-"}</td>
<td class="num">{f"{m['equity_ratio']:.0f}%" if m.get("equity_ratio") is not None else "-"}</td>
<td class="num">{_pct(dilution, signed=True)}{dilution_note}</td>
<td class="owners">{owners}{vc}</td>
</tr>"""


def build_html(run_date: str, rows, stats: Dict, fins: Optional[Dict] = None) -> str:
    fins = fins or {}
    body_rows = "\n".join(
        _row_html(r["rank"], r["code"], r["name"], r["sector33"] or r["market"],
                  r["score"], json.loads(r["metrics_json"]), fins.get(r["code"], ()))
        for r in rows)

    hero_html = ""
    if rows:
        top = rows[0]
        m = json.loads(top["metrics_json"])
        owners = " / ".join(_esc(o) for o in (m.get("owner_names") or [])) or "-"
        hero_html = f"""<div class="hero">
<div class="rank">本日の1位</div>
<div class="name"><a href="https://irbank.net/{_esc(top["code"])}" target="_blank" rel="noopener">{_esc(top["name"])}</a> ({_esc(top["code"])}/{_esc(top["market"])}) — スコア {top["score"]:.3f}</div>
<div class="metrics">売上CAGR {_pct(m.get("rev_cagr"))} ・ 連続増収増益 {m.get("consec_growth", "-")}期 ・ 営業CFマージン {f"{m['op_cf_margin']:.1f}%" if m.get("op_cf_margin") is not None else "-"} ・ 自己資本比率 {f"{m['equity_ratio']:.0f}%" if m.get("equity_ratio") is not None else "-"}</div>
<div class="biz">{_esc(m.get("business"))}</div>
<div class="owners">大株主: {owners}</div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kabu-screener {_esc(run_date)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
<h1>kabu-screener</h1>
<p class="sub">{_esc(run_date)} 実行 — 増収増益・CF良好・(できれば)オーナー大株主の発見機</p>
</header>
{_logic_html()}
<div class="tiles">
<div class="tile"><div class="v">{stats["candidates"]:,}</div><div class="k">候補(営業CF赤字なし)</div></div>
<div class="tile"><div class="v">{stats["companies"]:,}</div><div class="k">上場銘柄</div></div>
<div class="tile"><div class="v">{stats["edinet"]:,}</div><div class="k">EDINET収録社数</div></div>
</div>
{hero_html}
<div class="tablebox">
<table>
<thead><tr>
<th class="num">#</th><th>code</th><th>銘柄 / 事業</th><th class="num">スコア</th>
<th class="num">売上CAGR</th><th class="num">連続</th><th class="num">CF率</th>
<th class="num">自己資本</th><th class="num">株式数変化</th><th>大株主(個人)</th>
</tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
</div>
<footer>
生成: {datetime.now().strftime("%Y-%m-%d %H:%M")} / データ: EDINET(金融庁)・IR BANK・JPX /
スコアは全上場企業内パーセンタイルの加重平均。自分用スクリーナーであり投資勧誘ではない。
コードのリンク先はIR BANK。
</footer>
</div>
</body>
</html>
"""


def generate(conn, top: int) -> Optional[Path]:
    run_date = conn.execute(
        "SELECT MAX(run_date) FROM screen_results").fetchone()[0]
    if not run_date:
        print("screen_resultsが空です。先に src.screen を実行してください")
        return None
    rows = conn.execute(
        """SELECT s.rank, s.code, s.score, s.metrics_json,
                  c.name, c.market, c.sector33
           FROM screen_results s JOIN companies c ON c.code = s.code
           WHERE s.run_date = ? ORDER BY s.rank LIMIT ?""",
        (run_date, top)).fetchall()
    stats = {
        "candidates": conn.execute(
            "SELECT COUNT(*) FROM screen_results WHERE run_date = ?",
            (run_date,)).fetchone()[0],
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "edinet": conn.execute(
            "SELECT COUNT(DISTINCT code) FROM financials WHERE source='edinet'"
        ).fetchone()[0],
    }
    codes = [r["code"] for r in rows]
    fins: Dict[str, list] = {c: [] for c in codes}
    if codes:
        ph = ",".join("?" * len(codes))
        for f in conn.execute(
                f"""SELECT code, fiscal_year, is_forecast,
                           revenue, op_income, net_income, op_cf
                    FROM financials WHERE code IN ({ph})
                    ORDER BY code, fiscal_year""", codes):
            fins[f["code"]].append(dict(f))
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "index.html"
    out.write_text(build_html(run_date, rows, stats, fins), encoding="utf-8")
    print(f"{out} を生成しました({run_date}, {len(rows)}行)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()
    # 生成できなかったら非ゼロ終了(daily.shが古いページを再公開しないためのゲート)
    if generate(get_conn(), args.top) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
