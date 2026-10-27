#!/usr/bin/env python3
"""Render dashboard.html from trend.csv, brands.json and the latest run's summary.csv.

One static page, no server, no external scripts. Open it in a browser or host it
anywhere that serves a file.

  python3 dashboard.py                 # latest run
  python3 dashboard.py --out docs/index.html
"""

import argparse
import csv
import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")

PLATFORM_LABELS = {
    "openai": "ChatGPT", "gemini": "Gemini", "anthropic": "Claude",
    "perplexity": "Perplexity", "grok": "Grok",
}
# Fixed hue per platform (never cycled), light / dark.
PLATFORM_COLORS = {
    "openai": ("#2a78d6", "#3987e5"),
    "gemini": ("#eb6834", "#d95926"),
    "anthropic": ("#1baf7a", "#199e70"),
    "perplexity": ("#eda100", "#c98500"),
    "grok": ("#e87ba4", "#d55181"),
}


def load():
    with open(os.path.join(HERE, "brands.json")) as f:
        brands = json.load(f)["brands"]
    with open(os.path.join(HERE, "trend.csv")) as f:
        trend = list(csv.DictReader(f))
    dates = sorted({r["date"] for r in trend})
    latest = dates[-1]
    with open(os.path.join(RUNS, latest, "summary.csv")) as f:
        summary = list(csv.DictReader(f))
    return brands, trend, dates, latest, summary


def build_data(brands, trend, dates, latest, summary):
    own = [b["name"] for b in brands if b.get("own")]
    platforms = [p for p in PLATFORM_LABELS if any(r["platform"] == p for r in trend)]

    # Trend series: brand -> platform -> [{date, rate, mentions, tested}]
    series = defaultdict(lambda: defaultdict(dict))
    for r in trend:
        series[r["brand"]][r["platform"]][r["date"]] = {
            "rate": int(r["mention_rate"]) if r["mention_rate"] else None,
            "mentions": int(r["mentions"]), "tested": int(r["tested"]),
        }
    # Pooled rate per brand per date
    pooled = defaultdict(dict)
    for b in {r["brand"] for r in trend}:
        for d in dates:
            rows = [r for r in trend if r["brand"] == b and r["date"] == d]
            t = sum(int(r["tested"]) for r in rows)
            m = sum(int(r["mentions"]) for r in rows)
            pooled[b][d] = {"rate": round(100 * m / t) if t else None, "mentions": m, "tested": t}

    # Latest-run rank table over all brands
    prev = dates[-2] if len(dates) > 1 else None
    rank_table = []
    for b in brands:
        cur = pooled[b["name"]].get(latest, {"rate": None, "mentions": 0, "tested": 0})
        pv = pooled[b["name"]].get(prev, {"rate": None}) if prev else {"rate": None}
        rows = [r for r in trend if r["brand"] == b["name"] and r["date"] == latest]
        first = sum(int(r["named_first"]) for r in rows)
        ranked = [(float(r["avg_rank"]), int(r["mentions"])) for r in rows if r["avg_rank"]]
        avg = round(sum(a * n for a, n in ranked) / sum(n for _, n in ranked), 1) if ranked else None
        cited = sum(int(r["cited_answers"]) for r in rows)
        rank_table.append({
            "brand": b["name"], "own": bool(b.get("own")), "mentions": cur["mentions"],
            "tested": cur["tested"], "rate": cur["rate"], "prev_rate": pv["rate"],
            "named_first": first, "avg_rank": avg, "cited": cited,
        })
    rank_table.sort(key=lambda r: (-(r["rate"] or 0), r["brand"]))

    # Per query, latest run: for own brands, which platforms named them
    per_query = {}
    for r in summary:
        if r["own"] != "yes":
            continue
        q = per_query.setdefault(int(r["query_id"]), {
            "id": int(r["query_id"]), "text": r["query_text"], "category": r["category"],
            "cells": {},
        })
        q["cells"].setdefault(r["brand"], {})[r["platform"]] = {
            "mentioned": r["mentioned"] == "yes",
            "rank": int(r["rank"]) if r["rank"] else None,
            "cited": r["cited"] == "yes",
        }
    queries = sorted(per_query.values(), key=lambda q: q["id"])
    gaps = [q for q in queries if not any(c["mentioned"] for b in q["cells"].values() for c in b.values())]

    return {
        "latest": latest, "prev": prev, "dates": dates, "own": own, "platforms": platforms,
        "platform_labels": PLATFORM_LABELS, "platform_colors": PLATFORM_COLORS,
        "series": series, "pooled": pooled, "rank_table": rank_table,
        "queries": queries, "gaps": [q["id"] for q in gaps],
    }


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AEO tracker</title>
<style>
:root {
  --bg: #fcfcfb; --surface: #ffffff; --line: #e6e5e1; --grid: #eeedea;
  --text: #0b0b0b; --text-2: #52514e; --text-3: #8a8884;
  --own-bg: #f1f6fd; --good: #008300; --bad: #e34948;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1a1a19; --surface: #222221; --line: #33332f; --grid: #2b2b28;
    --text: #f0efec; --text-2: #c3c2b7; --text-3: #8d8c84; --own-bg: #1f2a3a;
    --good: #4cc04c; --bad: #e66767;
  }
}
:root[data-theme="dark"] {
  --bg: #1a1a19; --surface: #222221; --line: #33332f; --grid: #2b2b28;
  --text: #f0efec; --text-2: #c3c2b7; --text-3: #8d8c84; --own-bg: #1f2a3a;
  --good: #4cc04c; --bad: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 40px 0 12px; }
.sub { color: var(--text-2); margin: 0 0 24px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.tile { border: 1px solid var(--line); background: var(--surface); padding: 14px 16px; }
.tile .k { color: var(--text-2); font-size: 13px; }
.tile .v { font-size: 30px; font-weight: 600; line-height: 1.1; margin-top: 4px; }
.tile .d { font-size: 13px; color: var(--text-2); margin-top: 2px; }
.up { color: var(--good); } .down { color: var(--bad); }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.chart { border: 1px solid var(--line); background: var(--surface); padding: 12px 14px 8px; }
.chart h3 { margin: 0 0 6px; font-size: 15px; font-weight: 600; }
.chart svg { width: 100%; height: auto; display: block; overflow: visible; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 13px; color: var(--text-2); margin-top: 6px; }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--c); margin-right: 6px; vertical-align: -1px; }
.axis text { fill: var(--text-3); font-size: 11px; }
.grid line { stroke: var(--grid); }
.tip { position: fixed; pointer-events: none; background: var(--surface); border: 1px solid var(--line); padding: 8px 10px; font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,.12); display: none; z-index: 9; max-width: 320px; }
.tip b { font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 14px; background: var(--surface); border: 1px solid var(--line); }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: var(--text-2); font-weight: 500; font-size: 13px; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
tr.own td { background: var(--own-bg); font-weight: 600; }
.muted { color: var(--text-3); }
.cell { display: inline-block; min-width: 26px; text-align: center; font-variant-numeric: tabular-nums; }
.cell.no { color: var(--text-3); }
.cell.cited { text-decoration: underline; text-decoration-color: var(--text-3); }
.qt { max-width: 420px; }
.cat { color: var(--text-3); font-size: 12px; }
.note { color: var(--text-2); font-size: 13px; margin-top: 8px; }
</style>
</head>
<body>
<main>
<h1>AEO tracker</h1>
<p class="sub" id="sub"></p>
<div class="tiles" id="tiles"></div>

<h2>Mention rate by platform</h2>
<div class="charts" id="charts"></div>
<p class="note">Share of answers that name the brand in prose. One chart per brand; each line is a platform.</p>

<h2>All tracked brands, latest run</h2>
<div id="rank"></div>
<p class="note">Pooled across platforms. Rank is order of first appearance among tracked brands; a brand you do not track cannot push you down.</p>

<h2>Query by query, latest run</h2>
<div id="queries"></div>
<p class="note">Number = order in which the brand was named among tracked brands (1 = first). A dash means not named. Underlined = one of the brand's pages was among the answer's sources.</p>
</main>
<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light" || document.documentElement.dataset.theme === "dark";
const color = p => DATA.platform_colors[p][dark() ? 1 : 0];
const label = p => DATA.platform_labels[p] || p;
const fmtDate = d => { const [y,m,dd] = d.split("-"); return `${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+m-1]} ${+dd}`; };
const tip = document.getElementById("tip");
function showTip(html, x, y) { tip.innerHTML = html; tip.style.display = "block"; const r = tip.getBoundingClientRect(); tip.style.left = Math.min(x + 12, innerWidth - r.width - 8) + "px"; tip.style.top = Math.min(y + 12, innerHeight - r.height - 8) + "px"; }
function hideTip() { tip.style.display = "none"; }

// Header + tiles
const prevTxt = DATA.prev ? `, compared with ${fmtDate(DATA.prev)}` : "";
document.getElementById("sub").textContent = `Latest run ${DATA.latest}${prevTxt}. ${DATA.platforms.map(label).join(", ")}.`;
const tiles = document.getElementById("tiles");
for (const b of DATA.own) {
  const cur = DATA.pooled[b][DATA.latest], prev = DATA.prev ? DATA.pooled[b][DATA.prev] : null;
  let delta = "";
  if (prev && prev.rate != null && cur.rate != null) { const d = cur.rate - prev.rate; delta = `<span class="${d>0?"up":d<0?"down":""}">${d>0?"+":""}${d} pts</span> vs last run`; }
  tiles.insertAdjacentHTML("beforeend", `<div class="tile"><div class="k">${b}, pooled mention rate</div><div class="v">${cur.rate == null ? "n/a" : cur.rate + "%"}</div><div class="d">${cur.mentions} of ${cur.tested} answers. ${delta}</div></div>`);
}
tiles.insertAdjacentHTML("beforeend", `<div class="tile"><div class="k">Queries where no own brand is named</div><div class="v">${DATA.gaps.length}</div><div class="d">of ${DATA.queries.length} queries, on any platform</div></div>`);

// Line charts: one per own brand, series = platforms
const W = 560, H = 220, M = {l: 34, r: 12, t: 10, b: 26};
function lineChart(brand) {
  const dates = DATA.dates, n = dates.length;
  const x = i => M.l + (n === 1 ? (W - M.l - M.r) / 2 : (W - M.l - M.r) * i / (n - 1));
  const y = v => M.t + (H - M.t - M.b) * (1 - v / 100);
  let s = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">`;
  s += `<g class="grid">` + [0,25,50,75,100].map(v => `<line x1="${M.l}" x2="${W-M.r}" y1="${y(v)}" y2="${y(v)}"/>`).join("") + `</g>`;
  s += `<g class="axis">` + [0,50,100].map(v => `<text x="${M.l-6}" y="${y(v)+4}" text-anchor="end">${v}%</text>`).join("");
  const step = Math.max(1, Math.ceil(n / 8));
  s += dates.map((d,i) => (i % step === 0 || i === n-1) ? `<text x="${x(i)}" y="${H-8}" text-anchor="middle">${fmtDate(d)}</text>` : "").join("") + `</g>`;
  for (const p of DATA.platforms) {
    const ser = DATA.series[brand][p] || {};
    const pts = dates.map((d,i) => ser[d] && ser[d].rate != null ? [x(i), y(ser[d].rate)] : null);
    let path = "", pen = false;
    pts.forEach(pt => { if (!pt) { pen = false; return; } path += (pen ? "L" : "M") + pt[0].toFixed(1) + " " + pt[1].toFixed(1); pen = true; });
    s += `<path d="${path}" fill="none" stroke="${color(p)}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
    pts.forEach((pt,i) => { if (pt) s += `<circle cx="${pt[0]}" cy="${pt[1]}" r="4" fill="${color(p)}" stroke="var(--surface)" stroke-width="2" data-p="${p}" data-i="${i}" data-b="${brand}"/>`; });
  }
  s += `</svg>`;
  const leg = `<div class="legend">` + DATA.platforms.map(p => `<span style="--c:${color(p)}">${label(p)}</span>`).join("") + `</div>`;
  return `<div class="chart"><h3>${brand}</h3>${s}${leg}</div>`;
}
const charts = document.getElementById("charts");
charts.innerHTML = DATA.own.map(lineChart).join("");
charts.addEventListener("mousemove", e => {
  const c = e.target.closest("circle"); if (!c) { hideTip(); return; }
  const d = DATA.dates[+c.dataset.i], v = DATA.series[c.dataset.b][c.dataset.p][d];
  showTip(`<b>${c.dataset.b}</b> on ${label(c.dataset.p)}<br>${d}: ${v.rate}% (${v.mentions} of ${v.tested} answers)`, e.clientX, e.clientY);
});
charts.addEventListener("mouseleave", hideTip);

// Rank table
const rk = DATA.rank_table;
document.getElementById("rank").innerHTML = `<table><thead><tr><th class="n">#</th><th>Brand</th><th class="n">Mentions</th><th class="n">Rate</th><th class="n">Change</th><th class="n">Named first</th><th class="n">Avg rank when named</th><th class="n">Answers citing its pages</th></tr></thead><tbody>` +
  rk.map((r,i) => { const d = (r.prev_rate != null && r.rate != null) ? r.rate - r.prev_rate : null; const dt = d == null ? `<span class="muted">n/a</span>` : `<span class="${d>0?"up":d<0?"down":"muted"}">${d>0?"+":""}${d}</span>`;
    return `<tr class="${r.own?"own":""}"><td class="n">${i+1}</td><td>${r.brand}${r.own?" <span class='muted'>(you)</span>":""}</td><td class="n">${r.mentions} / ${r.tested}</td><td class="n">${r.rate==null?"n/a":r.rate+"%"}</td><td class="n">${dt}</td><td class="n">${r.named_first}</td><td class="n">${r.avg_rank==null?"<span class='muted'>n/a</span>":r.avg_rank}</td><td class="n">${r.cited}</td></tr>`; }).join("") + `</tbody></table>`;

// Query table
const own = DATA.own, plats = DATA.platforms;
let qh = `<table><thead><tr><th>Query</th>` + own.map(b => `<th colspan="${plats.length}">${b}</th>`).join("") + `</tr><tr><th></th>` + own.map(() => plats.map(p => `<th class="n">${label(p)}</th>`).join("")).join("") + `</tr></thead><tbody>`;
for (const q of DATA.queries) {
  qh += `<tr><td class="qt">Q${q.id}. ${q.text} <span class="cat">${q.category}</span></td>`;
  for (const b of own) for (const p of plats) {
    const c = (q.cells[b] || {})[p];
    if (!c) qh += `<td class="n"><span class="cell no">·</span></td>`;
    else qh += `<td class="n"><span class="cell ${c.mentioned?"":"no"} ${c.cited?"cited":""}" title="${b} on ${label(p)}: ${c.mentioned?"named #"+c.rank:"not named"}${c.cited?", page cited":""}">${c.mentioned ? c.rank : "–"}</span></td>`;
  }
  qh += `</tr>`;
}
document.getElementById("queries").innerHTML = qh + `</tbody></table>`;
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "dashboard.html"))
    args = ap.parse_args()
    data = build_data(*load())
    html = TEMPLATE.replace("__DATA__", json.dumps(data))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"wrote {args.out} ({data['latest']}, {len(data['dates'])} runs, {len(data['own'])} own brands)")


if __name__ == "__main__":
    main()
