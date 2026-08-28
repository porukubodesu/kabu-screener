"""スクリーニング結果の静的HTMLレポートを生成する。

data/site/index.html に自己完結のページ(CSS/JSインライン)を書き出す。
daily.sh がこれを site ブランチに載せてGitHub Pagesで公開する。

構成:
- ヒーローヘッダ+ロジック説明(screen.pyの定数から動的生成)
- 業種タブ(クリックで絞り込み)
- カード一覧(クリック不要で全情報をそのまま表示):
  スコア・時価総額・直近業績・指標チップ・事業内容・決算推移(表+ミニバー)・
  月足キャンドル(J-Quants日足を月次集約した自前SVG)・大株主。
  ※TradingView埋め込みは東証データをライセンス配信していないためリンクのみ

使い方:
  .venv/bin/python -m src.report            # 最新のscreen_resultsから生成
  .venv/bin/python -m src.report --top 300
"""
import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .db import DATA_DIR, get_conn
from .screen import (EXCLUDE_VC, MIN_OWNER_RATIO, NG_BUSINESS_KEYWORDS,
                     REQUIRE_OP_CF_NONNEG, WEIGHTS, _snippet)

SITE_DIR = DATA_DIR / "site"

WEIGHT_LABELS = {
    "rev_cagr": "売上CAGR", "consec_growth": "連続増収増益",
    "op_cf_margin": "営業CFマージン", "owner_ratio": "オーナー保有率",
    "equity_ratio": "自己資本比率", "anti_dilution": "非希薄化",
}

# 有報「事業の内容」冒頭の見出しゴミ(「3 【事業の内容】」等)を落とす
_BIZ_HEAD_RE = re.compile(r"^[0-9０-９]*\s*【事業の内容】\s*")

# 配色はdatavizスキルの検証済みリファレンスパレット(役割トークンのみ使用)
CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --accent-soft: rgba(42,120,214,0.10);
  --track: #f0efec; --wash: rgba(11,11,11,0.04);
  --shadow: 0 1px 2px rgba(11,11,11,0.05), 0 4px 16px rgba(11,11,11,0.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --accent-soft: rgba(57,135,229,0.16);
    --track: #383835; --wash: rgba(255,255,255,0.05);
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.65;
}
.wrap { max-width: 880px; margin: 0 auto; padding: 36px 18px 64px; }
.hero h1 { font-size: 30px; letter-spacing: -0.02em; margin: 0 0 4px; }
.hero h1 em { font-style: normal; color: var(--accent); }
.hero .lead { color: var(--ink-2); margin: 0; font-size: 14px; }
.pills { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 6px; }
.pill {
  border: 1px solid var(--border); background: var(--surface);
  border-radius: 999px; padding: 3px 12px; font-size: 12px; color: var(--ink-2);
}
.pill b { color: var(--ink); font-variant-numeric: tabular-nums; }
details.logic { margin: 10px 0 0; font-size: 13px; color: var(--ink-2); }
details.logic summary { cursor: pointer; color: var(--muted); }
details.logic .box {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 10px 16px; margin-top: 8px;
}
.tabs { display: flex; gap: 6px; overflow-x: auto; padding: 18px 0 4px; position: sticky; top: 0; background: var(--page); z-index: 5; }
.tab {
  flex: none; border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); color: var(--ink-2); font-size: 12px;
  padding: 4px 13px; cursor: pointer;
}
.tab .n { color: var(--muted); }
.tab.on { background: var(--ink); border-color: var(--ink); color: var(--page); font-weight: 600; }
.tab.on .n { color: var(--page); opacity: 0.7; }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; box-shadow: var(--shadow);
  padding: 18px 20px 14px; margin-top: 14px;
}
.chead { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.rank { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; min-width: 2.4em; }
.cname { font-size: 17px; font-weight: 700; }
.cname a { color: inherit; text-decoration: none; }
.cname a:hover { color: var(--accent); }
.ccode { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 2px; }
.tag {
  border: 1px solid var(--border); border-radius: 999px; color: var(--muted);
  font-size: 11px; padding: 1px 9px; vertical-align: 2px;
}
.cscore { margin-left: auto; text-align: right; }
.cscore b { font-size: 17px; color: var(--accent); font-variant-numeric: tabular-nums; }
.cscore .sl { color: var(--muted); font-size: 11px; margin-right: 4px; }
.scoretrack { height: 4px; background: var(--track); border-radius: 0 4px 4px 0; margin-top: 6px; }
.scoretrack i { display: block; height: 100%; background: var(--accent); border-radius: 0 4px 4px 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 0; }
.chip {
  background: var(--accent-soft); color: var(--ink-2); border-radius: 8px;
  font-size: 12px; padding: 3px 10px;
}
.chip b { color: var(--ink); font-variant-numeric: tabular-nums; font-weight: 600; }
.cbiz { color: var(--ink-2); font-size: 13px; margin: 12px 0 0; }
.cgrid { display: flex; flex-wrap: wrap; gap: 12px 32px; align-items: flex-start; margin-top: 12px; }
.fin table { border-collapse: collapse; }
.fin th {
  color: var(--muted); font-weight: 500; font-size: 11px; text-align: left;
  padding: 2px 14px 2px 0; border-bottom: 1px solid var(--grid);
}
.fin th.num { text-align: right; }
.fin td { color: var(--ink-2); font-size: 12px; padding: 2px 14px 2px 0;
  text-align: right; font-variant-numeric: tabular-nums; }
.fin td:first-child { text-align: left; }
.sl { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
.sparks { display: flex; gap: 20px; }
svg.spark rect { fill: var(--accent); }
svg.candles line.wick { stroke: var(--muted); stroke-width: 1; }
svg.candles rect.up { fill: var(--accent); }
svg.candles rect.down { fill: var(--muted); }
.cowners { color: var(--muted); font-size: 12px; margin-top: 12px; }
.cowners b { color: var(--ink-2); font-weight: 500; }
.cfoot { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; margin-top: 10px;
  border-top: 1px solid var(--grid); padding-top: 10px; }
.cfoot a { color: var(--muted); font-size: 12px; }
.cfoot a:hover { color: var(--accent); }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; }
"""

JS = """
var tabs = document.querySelectorAll(".tab");
tabs.forEach(function (t) {
  t.addEventListener("click", function () {
    tabs.forEach(function (x) { x.classList.toggle("on", x === t); });
    var s = t.dataset.sector;
    document.querySelectorAll(".card").forEach(function (c) {
      c.hidden = !!s && c.dataset.sector !== s;
    });
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


def clean_business(text: str) -> str:
    """有報の見出しゴミを落として本文から始める。"""
    return _BIZ_HEAD_RE.sub("", text.lstrip())


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


def monthly_candles(bars):
    """日足[(date,o,h,l,c)...] を月足に集約して [(YYYY-MM, o,h,l,c)...] を返す。"""
    months: Dict[str, list] = {}
    for d, o, h, l, c in bars:
        months.setdefault(d[:7], []).append((d, o, h, l, c))
    out = []
    for ym in sorted(months):
        rows = sorted(months[ym])
        o = next((r[1] for r in rows if r[1] is not None), None)
        c = next((r[4] for r in reversed(rows) if r[4] is not None), None)
        highs = [r[2] for r in rows if r[2] is not None]
        lows = [r[3] for r in rows if r[3] is not None]
        if o is None or c is None or not highs or not lows:
            continue
        out.append((ym, o, max(highs), min(lows), c))
    return out


def _candles_svg(monthly) -> str:
    """月足キャンドル(陽線=アクセント、陰線=グレー。符号は塗りの濃淡でも判別可)。"""
    monthly = monthly[-24:]
    if len(monthly) < 3:
        return ""
    hi = max(m[2] for m in monthly)
    lo = min(m[3] for m in monthly)
    span = (hi - lo) or 1
    w, gap, h = 6, 2, 64
    def y(v):
        return round((hi - v) / span * (h - 4) + 2, 1)
    parts = []
    x = 0
    for ym, o, hh, ll, c in monthly:
        cx = x + w / 2
        top, bottom = min(y(o), y(c)), max(y(o), y(c))
        body_h = max(bottom - top, 1)
        cls = "up" if c >= o else "down"
        parts.append(
            f'<g><title>{_esc(ym)}: 始{o:,.0f} 高{hh:,.0f} 安{ll:,.0f} 終{c:,.0f}</title>'
            f'<line class="wick" x1="{cx}" x2="{cx}" y1="{y(hh)}" y2="{y(ll)}"/>'
            f'<rect class="{cls}" x="{x}" y="{top}" width="{w}" height="{body_h}" rx="1"/></g>')
        x += w + gap
    total = x - gap
    return (f'<div><div class="sl">月足{len(monthly)}ヶ月(12週遅延)</div>'
            f'<svg class="candles" width="{total}" height="{h}" '
            f'viewBox="0 0 {total} {h}" role="img">{"".join(parts)}</svg></div>')


def _fin_table_html(fin_rows) -> str:
    if not fin_rows:
        return ""
    body = "\n".join(
        f"<tr><td>{_esc(f['fiscal_year'])}{'(予)' if f['is_forecast'] else ''}</td>"
        f"<td>{_yen(f['revenue'])}</td>"
        f"<td>{_yen(f['op_income'])}</td>"
        f"<td>{_yen(f['net_income'])}</td>"
        f"<td>{_yen(f['op_cf'])}</td></tr>"
        for f in fin_rows)
    return (f'<div class="fin"><table>'
            f'<tr><th>年度</th><th class="num">売上</th><th class="num">営利</th>'
            f'<th class="num">純利</th><th class="num">営業CF</th></tr>'
            f'{body}</table></div>')


def _card_html(r, m: Dict, fin_rows, biz: Optional[str],
               close: Optional[float], mktcap: Optional[float],
               bars=()) -> str:
    code, sector = r["code"], r["sector33"] or r["market"]
    # 時価総額はJ-Quantsの公式値を優先、無ければ終値×推定株式数で概算
    mcap = mktcap
    if mcap is None:
        shares = _shares(fin_rows)
        mcap = close * shares if (close and shares) else None
    dilution = m.get("dilution")
    dilution_note = "⚠" if dilution is not None and abs(dilution) > 0.5 else ""
    eq = m.get("equity_ratio")
    cfm = m.get("op_cf_margin")
    chips = [
        f'<span class="chip">時価総額 <b>{_yen(mcap)}</b></span>',
        f'<span class="chip">売上 <b>{_yen(_latest_actual(fin_rows, "revenue"))}</b></span>',
        f'<span class="chip">営利 <b>{_yen(_latest_actual(fin_rows, "op_income"))}</b></span>',
        f'<span class="chip">CAGR <b>{_pct(m.get("rev_cagr"))}</b></span>',
        f'<span class="chip">連続増収増益 <b>{m.get("consec_growth", "-")}期</b></span>',
        f'<span class="chip">自己資本 <b>{f"{eq:.0f}%" if eq is not None else "-"}</b></span>',
        f'<span class="chip">CF率 <b>{f"{cfm:.1f}%" if cfm is not None else "-"}</b></span>',
        f'<span class="chip" title="株式数変化(分割境界の誤差あり得る)">株式数 <b>{_pct(dilution, signed=True)}{dilution_note}</b></span>',
    ]
    owners = " / ".join(_esc(o) for o in (m.get("owner_names") or [])) or "個人大株主なし"
    vc_tag = '<span class="tag">VC</span>' if m.get("has_vc") else ""
    vc_line = (f' ・ VC: {_esc(" / ".join(m.get("vc_names") or []))}'
               if m.get("has_vc") else "")
    biz_html = f'<p class="cbiz">{_esc(biz)}</p>' if biz else ""
    sparks = ""
    rev_s, op_s = _bars_svg(fin_rows, "revenue"), _bars_svg(fin_rows, "op_income")
    candle_s = _candles_svg(monthly_candles(bars)) if bars else ""
    if rev_s or op_s or candle_s:
        sparks = ('<div class="sparks">'
                  + (f'<div><div class="sl">売上</div>{rev_s}</div>' if rev_s else "")
                  + (f'<div><div class="sl">営業利益</div>{op_s}</div>' if op_s else "")
                  + candle_s
                  + "</div>")
    c = _esc(code)
    return f"""<article class="card" data-sector="{_esc(sector)}">
<div class="chead">
<span class="rank">#{r["rank"]}</span>
<span class="cname"><a href="https://irbank.net/{c}" target="_blank" rel="noopener">{_esc(r["name"])}</a><span class="ccode">{c}</span></span>
<span class="tag">{_esc(sector)}</span>{vc_tag}
<span class="cscore"><span class="sl">score</span><b>{r["score"]:.3f}</b></span>
</div>
<div class="scoretrack"><i style="width:{r["score"] * 100:.0f}%"></i></div>
<div class="chips">{"".join(chips)}</div>
{biz_html}
<div class="cgrid">{_fin_table_html(fin_rows)}{sparks}</div>
<div class="cowners"><b>オーナー</b> {owners}{vc_line} ・ 時点 {_esc(m.get("holder_as_of") or "-")}</div>
<div class="cfoot">
<a href="https://irbank.net/{c}" target="_blank" rel="noopener">IR BANK</a>
<a href="https://kabutan.jp/stock/?code={c}" target="_blank" rel="noopener">株探</a>
<a href="https://www.tradingview.com/chart/?symbol=TSE%3A{c}" target="_blank" rel="noopener">TradingView</a>
</div>
</article>"""


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
    return f"""<details class="logic">
<summary>スクリーニングのロジック</summary>
<div class="box">
① 候補条件は「{"・".join(conds) or "なし"}」と事業内容のNGワード({_esc(ng)})除外のみ。
② スコアは各指標の<b>全上場企業内パーセンタイル</b>(0〜1)の加重平均 — {weights}。
オーナー保有・VC不在は必須条件にせず、スコアと表示で判断材料にする。
③ 判定機ではなく<b>発見機</b> — 上位から人間が四季報などで定性確認する前提。閾値の発明はしない。
</div>
</details>"""


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
               prices: Optional[Dict] = None,
               bars: Optional[Dict] = None) -> str:
    fins = fins or {}
    businesses = businesses or {}
    prices = prices or {}
    bars = bars or {}
    cards = "\n".join(
        _card_html(r, json.loads(r["metrics_json"]), fins.get(r["code"], ()),
                   businesses.get(r["code"]),
                   prices.get(r["code"], (None, None, None))[1],
                   prices.get(r["code"], (None, None, None))[2],
                   bars.get(r["code"], ()))
        for r in rows)
    price_dates = sorted({v[0] for v in prices.values()}) if prices else []
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
<div class="hero">
<h1>kabu-<em>screener</em></h1>
<p class="lead">増収増益・キャッシュフロー良好・(できれば)オーナーが大株主 — 全上場企業からの発見機</p>
<div class="pills">
<span class="pill">実行 <b>{_esc(run_date)}</b></span>
<span class="pill">候補 <b>{stats["candidates"]:,}</b>社 / 上場 {stats["companies"]:,}社</span>
<span class="pill">表示 上位<b>{len(rows)}</b>社</span>
<span class="pill">{_esc(price_note)}</span>
</div>
{_logic_html()}
</div>
{_tabs_html(rows)}
{cards}
<footer>
生成: {datetime.now().strftime("%Y-%m-%d %H:%M")} / データ: EDINET(金融庁)・IR BANK・JPX・J-Quants(終値・時価総額、無料プランは12週遅延) /
スコアは全上場企業内パーセンタイルの加重平均。自分用スクリーナーであり投資勧誘ではない。
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
    bars: Dict[str, list] = {}
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
            businesses[b["code"]] = _snippet(clean_business(b["description"]), 340)
        for p in conn.execute(
                f"SELECT code, date, close, mktcap FROM prices WHERE code IN ({ph})",
                codes):
            prices[p["code"]] = (p["date"], p["close"], p["mktcap"])
        for b in conn.execute(
                f"""SELECT code, date, open, high, low, close FROM price_bars
                    WHERE code IN ({ph}) ORDER BY code, date""", codes):
            bars.setdefault(b["code"], []).append(
                (b["date"], b["open"], b["high"], b["low"], b["close"]))
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "index.html"
    out.write_text(
        build_html(run_date, rows, stats, fins, businesses, prices, bars),
        encoding="utf-8")
    print(f"{out} を生成しました({run_date}, {len(rows)}行)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300)
    args = ap.parse_args()
    # 生成できなかったら非ゼロ終了(daily.shが古いページを再公開しないためのゲート)
    if generate(get_conn(), args.top) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
