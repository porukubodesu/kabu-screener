"""スクリーニング結果の静的HTMLレポートを生成する。

data/site/index.html に自己完結のページを書き出す(CSS/JSインライン)。
daily.sh がこれを site ブランチに載せてGitHub Pagesで公開する。

構成:
- ロジック説明(screen.pyの定数から動的生成)
- 業種タブ(クリックで絞り込み)
- 一覧: スコア・時価総額(J-Quants終値×推定株式数の概算)・直近業績・指標
- 行クリックで詳細パネル: 事業内容全文スニペット・決算推移(表+バー)・大株主・
  月足チャート(TradingView埋め込み、クリック時のみロード)

使い方:
  .venv/bin/python -m src.report            # 最新のscreen_resultsから生成
  .venv/bin/python -m src.report --top 100
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
                     REQUIRE_OP_CF_NONNEG, WEIGHTS, _snippet)

SITE_DIR = DATA_DIR / "site"
COLS = 11  # 一覧の列数(詳細パネルのcolspan)

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
  --bar: #2a78d6; --bar-track: #f0efec; --wash: rgba(11,11,11,0.04);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --bar: #3987e5; --bar-track: #383835; --wash: rgba(255,255,255,0.05);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.6;
}
.wrap { max-width: 1160px; margin: 0 auto; padding: 20px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; }
header h1 { font-size: 20px; margin: 0; }
header .sub { color: var(--ink-2); margin: 0; font-size: 13px; }
.logic {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 16px; margin: 14px 0;
  font-size: 13px; color: var(--ink-2);
}
.logic b { color: var(--ink); }
.tabs { display: flex; gap: 6px; overflow-x: auto; padding: 2px 0 10px; }
.tab {
  flex: none; border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); color: var(--ink-2); font-size: 12px;
  padding: 3px 12px; cursor: pointer;
}
.tab .n { color: var(--muted); }
.tab.on { border-color: var(--ink); color: var(--ink); font-weight: 600; }
.hint { color: var(--muted); font-size: 12px; margin: 0 0 6px; }
.tablebox {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow-x: auto;
}
table.main { border-collapse: collapse; width: 100%; min-width: 1020px; }
table.main > thead th {
  text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
  padding: 10px 8px; border-bottom: 1px solid var(--grid);
  position: sticky; top: 0; background: var(--surface); z-index: 1;
}
table.main > tbody td { padding: 9px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr.r { cursor: pointer; }
tr.r:hover { background: var(--wash); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.code a { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--muted); }
.co { font-weight: 600; }
.sector { color: var(--muted); font-size: 11px; margin-left: 4px; }
.scorebar {
  display: inline-block; width: 64px; height: 6px; background: var(--bar-track);
  border-radius: 0 4px 4px 0; vertical-align: 2px; margin-left: 8px;
}
.scorebar i { display: block; height: 100%; background: var(--bar);
  border-radius: 0 4px 4px 0; }
.biz {
  color: var(--muted); font-size: 12px; line-height: 1.5; margin-top: 2px;
  display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden;
}
.owners { font-size: 12px; color: var(--ink-2); white-space: nowrap; }
.vcchip {
  display: inline-block; border: 1px solid var(--border); border-radius: 4px;
  color: var(--muted); font-size: 11px; padding: 0 4px; margin-left: 4px;
}
.warn { color: var(--muted); font-size: 11px; }
tr.panel > td { background: var(--wash); padding: 14px 16px 16px; }
.bizfull { color: var(--ink-2); font-size: 13px; max-width: 72em; margin: 0 0 10px; }
.meta { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
.panelflex { display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start; }
.fin table { border-collapse: collapse; }
.fin th {
  color: var(--muted); font-weight: 500; font-size: 11px; text-align: left;
  padding: 2px 14px 2px 0; border-bottom: 1px solid var(--grid);
}
.fin td { color: var(--ink-2); font-size: 12px; padding: 2px 14px 2px 0; }
.sl { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
.sparks { display: flex; gap: 20px; }
svg.spark rect { fill: var(--bar); }
.pholders { font-size: 12px; color: var(--ink-2); max-width: 60em; }
.plinks { margin-top: 10px; font-size: 12px; }
.plinks a { color: var(--ink-2); margin-right: 14px; }
button.loadchart {
  margin-top: 10px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--ink); font-size: 12px;
  padding: 5px 12px; cursor: pointer;
}
button.loadchart:hover { background: var(--wash); }
iframe.tvchart { width: 100%; height: 420px; border: 0; margin-top: 10px; border-radius: 8px; }
footer { color: var(--muted); font-size: 12px; margin-top: 20px; }
"""

JS = """
document.querySelectorAll("tr.r").forEach(function (tr) {
  tr.addEventListener("click", function (e) {
    if (e.target.closest("a, button")) return;
    var p = tr.nextElementSibling;
    if (p && p.classList.contains("panel")) p.hidden = !p.hidden;
  });
});
var tabs = document.querySelectorAll(".tab");
tabs.forEach(function (t) {
  t.addEventListener("click", function () {
    tabs.forEach(function (x) { x.classList.toggle("on", x === t); });
    var s = t.dataset.sector;
    document.querySelectorAll("tr.r").forEach(function (tr) {
      var show = !s || tr.dataset.sector === s;
      tr.hidden = !show;
      var p = tr.nextElementSibling;
      if (p && p.classList.contains("panel")) p.hidden = true;
    });
  });
});
document.querySelectorAll("button.loadchart").forEach(function (b) {
  b.addEventListener("click", function () {
    var dark = matchMedia("(prefers-color-scheme: dark)").matches;
    var f = document.createElement("iframe");
    f.className = "tvchart";
    f.loading = "lazy";
    f.src = "https://s.tradingview.com/widgetembed/?symbol=TSE%3A" +
      encodeURIComponent(b.dataset.code) +
      "&interval=M&style=1&locale=ja&timezone=Asia%2FTokyo" +
      "&hidesidetoolbar=1&symboledit=0&saveimage=0&withdateranges=1" +
      "&theme=" + (dark ? "dark" : "light");
    b.replaceWith(f);
  });
});
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


def _shares(fin_rows) -> Optional[float]:
    """推定株式数 = (株主資本 or 純資産)/BPS の直近値(screen.pyの希薄化と同じ近似)。"""
    for f in reversed(fin_rows):
        if f["is_forecast"]:
            continue
        base = f.get("shareholders_equity") or f.get("net_assets")
        bps = f.get("bps")
        if base and bps:
            return base / bps
    return None


def _latest_actual(fin_rows, key: str) -> Optional[float]:
    for f in reversed(fin_rows):
        if not f["is_forecast"] and f.get(key) is not None:
            return f[key]
    return None


def _bars_svg(fin_rows, key: str) -> str:
    """実績の推移ミニバー。負値は基準線の下に描く(符号は位置で伝える)。"""
    pts = [(f["fiscal_year"], f.get(key)) for f in fin_rows
           if not f["is_forecast"]][-6:]
    vals = [v for _, v in pts if v is not None]
    if len(vals) < 2:
        return ""
    mx = max(abs(v) for v in vals) or 1
    has_neg = any(v < 0 for v in vals)
    w, gap, h = 14, 2, 44
    base = h // 2 if has_neg else h - 1
    amp = h // 2 - 2 if has_neg else h - 3
    rects = []
    x = 0
    for fy, v in pts:
        if v is not None:
            bh = max(2, round(abs(v) / mx * amp))
            y = base - bh if v >= 0 else base
            rects.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{bh}" rx="2">'
                f'<title>{_esc(fy)}: {_yen(v)}</title></rect>')
        x += w + gap
    total = x - gap
    return (f'<svg class="spark" width="{total}" height="{h}" '
            f'viewBox="0 0 {total} {h}" role="img">'
            f'<line x1="0" x2="{total}" y1="{base}" y2="{base}" '
            f'stroke="var(--grid)" stroke-width="1"/>{"".join(rects)}</svg>')


def _fin_table_html(fin_rows) -> str:
    if not fin_rows:
        return ""
    body = "\n".join(
        f"<tr><td>{_esc(f['fiscal_year'])}{'(予)' if f['is_forecast'] else ''}</td>"
        f"<td class=\"num\">{_yen(f['revenue'])}</td>"
        f"<td class=\"num\">{_yen(f['op_income'])}</td>"
        f"<td class=\"num\">{_yen(f['net_income'])}</td>"
        f"<td class=\"num\">{_yen(f['op_cf'])}</td></tr>"
        for f in fin_rows)
    return (f'<div class="fin"><table>'
            f'<tr><th>年度</th><th class="num">売上</th><th class="num">営利</th>'
            f'<th class="num">純利</th><th class="num">営業CF</th></tr>'
            f'{body}</table></div>')


def _panel_html(code: str, m: Dict, fin_rows, biz_full: Optional[str]) -> str:
    dilution = m.get("dilution")
    dilution_note = (" ⚠分割の境界誤差の可能性あり"
                     if dilution is not None and abs(dilution) > 0.5 else "")
    eq = m.get("equity_ratio")
    eq_s = f"{eq:.0f}%" if eq is not None else "-"
    meta = (f"自己資本比率 {eq_s}"
            f" ・ 株式数変化 {_pct(dilution, signed=True)}{dilution_note}"
            f" ・ 大株主データ時点 {_esc(m.get('holder_as_of') or '-')}")
    owners = " / ".join(_esc(o) for o in (m.get("owner_names") or [])) or "個人大株主なし"
    vc = (f"<br>VC・ファンド: {_esc(' / '.join(m.get('vc_names') or []))}"
          if m.get("has_vc") else "")
    biz = (f'<p class="bizfull">{_esc(biz_full)}</p>' if biz_full else "")
    sparks = ""
    rev_s, op_s = _bars_svg(fin_rows, "revenue"), _bars_svg(fin_rows, "op_income")
    if rev_s or op_s:
        sparks = ('<div class="sparks">'
                  + (f'<div><div class="sl">売上</div>{rev_s}</div>' if rev_s else "")
                  + (f'<div><div class="sl">営業利益</div>{op_s}</div>' if op_s else "")
                  + "</div>")
    c = _esc(code)
    return f"""<tr class="panel" hidden><td colspan="{COLS}">
{biz}
<p class="meta">{meta}</p>
<div class="panelflex">{_fin_table_html(fin_rows)}{sparks}
<div class="pholders">大株主(個人): {owners}{vc}</div></div>
<button class="loadchart" data-code="{c}">📈 月足チャートを表示</button>
<div class="plinks">
<a href="https://irbank.net/{c}" target="_blank" rel="noopener">IR BANK</a>
<a href="https://kabutan.jp/stock/?code={c}" target="_blank" rel="noopener">株探</a>
<a href="https://www.tradingview.com/chart/?symbol=TSE%3A{c}" target="_blank" rel="noopener">TradingView</a>
</div>
</td></tr>"""


def _row_html(r, m: Dict, fin_rows, biz_full: Optional[str],
              close: Optional[float]) -> str:
    code, sector = r["code"], r["sector33"] or r["market"]
    shares = _shares(fin_rows)
    mcap = close * shares if (close and shares) else None
    top_owner = (m.get("owner_names") or ["-"])[0]
    vc = '<span class="vcchip">VC</span>' if m.get("has_vc") else ""
    biz_line = (f'<div class="biz">{_esc(m.get("business"))}</div>'
                if m.get("business") else "")
    main = f"""<tr class="r" data-sector="{_esc(sector)}">
<td class="num">{r["rank"]}</td>
<td class="code"><a href="https://irbank.net/{_esc(code)}" target="_blank" rel="noopener">{_esc(code)}</a></td>
<td><span class="co">{_esc(r["name"])}</span><span class="sector">{_esc(sector)}</span>{vc}{biz_line}</td>
<td class="num">{r["score"]:.3f}<span class="scorebar"><i style="width:{r["score"] * 100:.0f}%"></i></span></td>
<td class="num">{_yen(mcap)}</td>
<td class="num">{_yen(_latest_actual(fin_rows, "revenue"))}</td>
<td class="num">{_yen(_latest_actual(fin_rows, "op_income"))}</td>
<td class="num">{_pct(m.get("rev_cagr"))}</td>
<td class="num">{m.get("consec_growth", "-")}期</td>
<td class="num">{f"{m['op_cf_margin']:.1f}%" if m.get("op_cf_margin") is not None else "-"}</td>
<td class="owners">{_esc(top_owner)}</td>
</tr>"""
    return main + "\n" + _panel_html(code, m, fin_rows, biz_full)


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


def _tabs_html(rows) -> str:
    counts: Dict[str, int] = {}
    for r in rows:
        s = r["sector33"] or r["market"]
        counts[s] = counts.get(s, 0) + 1
    chips = [f'<button class="tab on" data-sector="">全て <span class="n">{len(rows)}</span></button>']
    for s, n in sorted(counts.items(), key=lambda t: -t[1]):
        chips.append(f'<button class="tab" data-sector="{_esc(s)}">'
                     f'{_esc(s)} <span class="n">{n}</span></button>')
    return '<div class="tabs">' + "".join(chips) + "</div>"


def build_html(run_date: str, rows, stats: Dict, fins: Optional[Dict] = None,
               businesses: Optional[Dict] = None,
               prices: Optional[Dict] = None) -> str:
    fins = fins or {}
    businesses = businesses or {}
    prices = prices or {}
    body_rows = "\n".join(
        _row_html(r, json.loads(r["metrics_json"]), fins.get(r["code"], ()),
                  businesses.get(r["code"]),
                  prices.get(r["code"], (None, None))[1])
        for r in rows)
    price_dates = sorted({d for d, _ in prices.values()}) if prices else []
    price_note = f"終値 {price_dates[-1]} 時点" if price_dates else "株価未取得"

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
<p class="sub">{_esc(run_date)} 実行 ・ 候補 {stats["candidates"]:,}社 / 上場 {stats["companies"]:,}社 ・ {_esc(price_note)}</p>
</header>
{_logic_html()}
{_tabs_html(rows)}
<p class="hint">行をクリックすると事業内容・決算推移・大株主・月足チャートが開きます</p>
<div class="tablebox">
<table class="main">
<thead><tr>
<th class="num">#</th><th>code</th><th>銘柄 / 事業</th><th class="num">スコア</th>
<th class="num">時価総額</th><th class="num">売上(直近)</th><th class="num">営利(直近)</th>
<th class="num">売上CAGR</th><th class="num">連続</th><th class="num">CF率</th><th>筆頭オーナー</th>
</tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
</div>
<footer>
生成: {datetime.now().strftime("%Y-%m-%d %H:%M")} / データ: EDINET(金融庁)・IR BANK・JPX・J-Quants(終値、無料プランは12週遅延) /
時価総額は終値×推定株式数(純資産÷BPS)の概算。スコアは全上場企業内パーセンタイルの加重平均。
自分用スクリーナーであり投資勧誘ではない。
</footer>
</div>
<script>{JS}</script>
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
    }
    codes = [r["code"] for r in rows]
    fins: Dict[str, list] = {c: [] for c in codes}
    businesses: Dict[str, str] = {}
    prices: Dict[str, tuple] = {}
    if codes:
        ph = ",".join("?" * len(codes))
        for f in conn.execute(
                f"""SELECT code, fiscal_year, is_forecast, revenue, op_income,
                           net_income, op_cf, shareholders_equity, net_assets, bps
                    FROM financials WHERE code IN ({ph})
                    ORDER BY code, fiscal_year""", codes):
            fins[f["code"]].append(dict(f))
        for b in conn.execute(
                f"SELECT code, description FROM business WHERE code IN ({ph})", codes):
            businesses[b["code"]] = _snippet(b["description"], 600)
        for p in conn.execute(
                f"SELECT code, date, close FROM prices WHERE code IN ({ph})", codes):
            prices[p["code"]] = (p["date"], p["close"])
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "index.html"
    out.write_text(
        build_html(run_date, rows, stats, fins, businesses, prices),
        encoding="utf-8")
    print(f"{out} を生成しました({run_date}, {len(rows)}行)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    args = ap.parse_args()
    # 生成できなかったら非ゼロ終了(daily.shが古いページを再公開しないためのゲート)
    if generate(get_conn(), args.top) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
