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
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .db import DATA_DIR, get_conn

SITE_DIR = DATA_DIR / "site"

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
.tablebox {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow-x: auto;
}
table { border-collapse: collapse; width: 100%; min-width: 860px; }
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


def _row_html(rank: int, code: str, name: str, sector: str, score: float,
              m: Dict) -> str:
    dilution = m.get("dilution")
    # 分割イベントの境界誤差の可能性が高い値には注記を付ける(既知の制約)
    dilution_note = (' <span class="warn" title="株式分割の境界誤差の可能性">⚠分割?</span>'
                     if dilution is not None and abs(dilution) > 0.5 else "")
    owners = " ".join(_esc(o) for o in (m.get("owner_names") or [])) or "-"
    vc = (f'<span class="chip" title="{_esc(" / ".join(m.get("vc_names") or []))}">VC</span>'
          if m.get("has_vc") else "")
    biz = f'<div class="biz">{_esc(m.get("business"))}</div>' if m.get("business") else ""
    return f"""<tr>
<td class="num">{rank}</td>
<td class="code"><a href="https://irbank.net/{_esc(code)}" target="_blank" rel="noopener">{_esc(code)}</a></td>
<td><span class="co">{_esc(name)}</span> <span class="sector">{_esc(sector)}</span>{biz}</td>
<td class="num">{score:.3f}<span class="scorebar"><i style="width:{score * 100:.0f}%"></i></span></td>
<td class="num">{_pct(m.get("rev_cagr"))}</td>
<td class="num">{m.get("consec_growth", "-")}期</td>
<td class="num">{f"{m['op_cf_margin']:.1f}%" if m.get("op_cf_margin") is not None else "-"}</td>
<td class="num">{f"{m['equity_ratio']:.0f}%" if m.get("equity_ratio") is not None else "-"}</td>
<td class="num">{_pct(dilution, signed=True)}{dilution_note}</td>
<td class="owners">{owners}{vc}</td>
</tr>"""


def build_html(run_date: str, rows, stats: Dict) -> str:
    body_rows = "\n".join(
        _row_html(r["rank"], r["code"], r["name"], r["sector33"] or r["market"],
                  r["score"], json.loads(r["metrics_json"]))
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
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "index.html"
    out.write_text(build_html(run_date, rows, stats), encoding="utf-8")
    print(f"{out} を生成しました({run_date}, {len(rows)}行)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()
    generate(get_conn(), args.top)


if __name__ == "__main__":
    main()
