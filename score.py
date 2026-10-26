#!/usr/bin/env python3
"""Score one collected run: who gets named, in what order, and whose pages get cited.

Reads runs/<date>/<platform>/q<id>.json (written by collect.sh), brands.json and
queries.json. Writes:

  runs/<date>/summary.csv   one row per (query, platform, brand)
  runs/<date>/report.md     the run at a glance, per brand and per platform
  trend.csv                 one row per (date, platform, brand), appended/replaced per run

Definitions (see README):
  mention   the brand name appears in the answer PROSE, after citation markup is
            blanked out. A URL the model merely read is not a mention.
  rank      order of first appearance among the brands in brands.json (1 = named
            first). Only tracked brands count, so a rank is relative to your list.
  cited     at least one source URL the model retrieved is on one of the brand's
            domains.

Usage:
  python3 score.py --date 2026-11-03
  python3 score.py --date 2026-11-03 --compare     # also diff against the previous run
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

from extractors import (KNOWN_PLATFORMS, extract_citation_urls, extract_text,
                        strip_citation_markup, url_domain)

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")


def load_brands():
    with open(os.path.join(HERE, "brands.json")) as f:
        cfg = json.load(f)
    brands = []
    for b in cfg["brands"]:
        names = [b["name"]] + b.get("aliases", [])
        # Whole-word, case-insensitive. Longest alias first so "Blake's Hard Cider"
        # wins over "Blake's" when both are listed.
        names.sort(key=len, reverse=True)
        pattern = re.compile(
            r"(?<![\w'])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w])",
            re.IGNORECASE,
        )
        brands.append({
            "name": b["name"],
            "own": bool(b.get("own", False)),
            "domains": [d.lower().lstrip("www.") for d in b.get("domains", [])],
            "pattern": pattern,
        })
    return brands


def load_queries():
    with open(os.path.join(HERE, "queries.json")) as f:
        qs = json.load(f)["queries"]
    return {q["id"]: q for q in qs if q.get("status", "active") != "retired"}


def domain_matches(domain, brand_domains):
    domain = (domain or "").lower().lstrip("www.")
    return any(domain == d or domain.endswith("." + d) for d in brand_domains)


def score_answer(text, urls, brands):
    """Return {brand_name: {mentioned, first_pos, rank, cited}} for one answer."""
    clean = strip_citation_markup(text)
    rows = {}
    firsts = []
    for b in brands:
        m = b["pattern"].search(clean)
        pos = m.start() if m else None
        cited = any(domain_matches(url_domain(u), b["domains"]) for u in urls)
        rows[b["name"]] = {"mentioned": pos is not None, "first_pos": pos, "cited": cited}
        if pos is not None:
            firsts.append((pos, b["name"]))
    firsts.sort()
    for rank, (_, name) in enumerate(firsts, start=1):
        rows[name]["rank"] = rank
    for r in rows.values():
        r.setdefault("rank", None)
    return rows


def score_run(date):
    brands = load_brands()
    queries = load_queries()
    run_dir = os.path.join(RUNS, date)
    if not os.path.isdir(run_dir):
        sys.exit(f"no run at {run_dir}")

    rows = []
    tested = defaultdict(int)
    for platform in KNOWN_PLATFORMS:
        pdir = os.path.join(run_dir, platform)
        if not os.path.isdir(pdir):
            continue
        for fname in sorted(os.listdir(pdir)):
            m = re.fullmatch(r"q(\d+)\.json", fname)
            if not m:
                continue
            qid = int(m.group(1))
            if qid not in queries:
                continue
            with open(os.path.join(pdir, fname)) as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    continue
            text = extract_text(data, platform)
            if not text.strip():
                continue
            urls = extract_citation_urls(data, platform, date)
            tested[platform] += 1
            scored = score_answer(text, urls, brands)
            for b in brands:
                s = scored[b["name"]]
                rows.append({
                    "date": date,
                    "query_id": qid,
                    "query_text": queries[qid]["text"],
                    "category": queries[qid].get("category", ""),
                    "platform": platform,
                    "brand": b["name"],
                    "own": "yes" if b["own"] else "no",
                    "mentioned": "yes" if s["mentioned"] else "no",
                    "rank": s["rank"] if s["rank"] is not None else "",
                    "cited": "yes" if s["cited"] else "no",
                    "sources_total": len(urls),
                })
    if not rows:
        sys.exit(f"no answers found under {run_dir}")

    fields = list(rows[0].keys())
    with open(os.path.join(run_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # Roll up per (platform, brand) and per brand overall.
    agg = defaultdict(lambda: {"tested": 0, "mentions": 0, "first": 0, "cited": 0, "rank_sum": 0, "ranked": 0})
    for r in rows:
        k = (r["platform"], r["brand"])
        a = agg[k]
        a["tested"] += 1
        if r["mentioned"] == "yes":
            a["mentions"] += 1
            a["rank_sum"] += int(r["rank"])
            a["ranked"] += 1
            if r["rank"] == 1:
                a["first"] += 1
        if r["cited"] == "yes":
            a["cited"] += 1

    trend_rows = []
    for (platform, brand), a in sorted(agg.items()):
        trend_rows.append({
            "date": date, "platform": platform, "brand": brand,
            "tested": a["tested"], "mentions": a["mentions"],
            "mention_rate": round(100 * a["mentions"] / a["tested"]) if a["tested"] else "",
            "named_first": a["first"],
            "avg_rank": round(a["rank_sum"] / a["ranked"], 1) if a["ranked"] else "",
            "cited_answers": a["cited"],
        })
    write_trend(date, trend_rows)
    write_report(date, brands, tested, trend_rows, rows)
    return trend_rows


def write_trend(date, new_rows):
    path = os.path.join(HERE, "trend.csv")
    kept = []
    if os.path.exists(path):
        with open(path) as f:
            kept = [r for r in csv.DictReader(f) if r["date"] != date]
    allrows = kept + new_rows
    allrows.sort(key=lambda r: (r["date"], r["platform"], r["brand"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(new_rows[0].keys()))
        w.writeheader()
        w.writerows(allrows)


def previous_run(date):
    dates = sorted(d for d in os.listdir(RUNS) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < date)
    return dates[-1] if dates else None


def load_trend_for(date):
    path = os.path.join(HERE, "trend.csv")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {(r["platform"], r["brand"]): r for r in csv.DictReader(f) if r["date"] == date}


def write_report(date, brands, tested, trend_rows, rows):
    prev_date = previous_run(date)
    prev = load_trend_for(prev_date) if prev_date else {}
    own = [b["name"] for b in brands if b["own"]]
    platforms = [p for p in KNOWN_PLATFORMS if tested.get(p)]

    out = [f"# AEO run {date}", ""]
    out.append("Answers collected: " + ", ".join(f"{p} {tested[p]}" for p in platforms))
    out.append("")

    # Own brands, pooled across platforms.
    out.append("## Your brands, all platforms pooled")
    out.append("")
    out.append("| Brand | Answers | Mentions | Rate | Named first | Cited |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for name in own:
        t = sum(int(r["tested"]) for r in trend_rows if r["brand"] == name)
        m = sum(int(r["mentions"]) for r in trend_rows if r["brand"] == name)
        f1 = sum(int(r["named_first"]) for r in trend_rows if r["brand"] == name)
        c = sum(int(r["cited_answers"]) for r in trend_rows if r["brand"] == name)
        out.append(f"| {name} | {t} | {m} | {round(100*m/t) if t else 0}% | {f1} | {c} |")
    out.append("")

    # Per platform, own brands, with change vs previous run.
    out.append("## Per platform")
    out.append("")
    hdr = "| Platform | " + " | ".join(own) + " |"
    out.append(hdr)
    out.append("|---|" + "---:|" * len(own))
    for p in platforms:
        cells = []
        for name in own:
            cur = next((r for r in trend_rows if r["platform"] == p and r["brand"] == name), None)
            if not cur:
                cells.append("")
                continue
            cell = f"{cur['mentions']}/{cur['tested']} ({cur['mention_rate']}%)"
            pr = prev.get((p, name))
            if pr:
                cell += f", was {pr['mentions']}/{pr['tested']}"
            cells.append(cell)
        out.append(f"| {p} | " + " | ".join(cells) + " |")
    out.append("")
    if prev_date:
        out.append(f"Change is against the previous run, {prev_date}.")
        out.append("")

    # Competitive rank: every tracked brand by pooled mention rate.
    out.append("## All tracked brands, pooled mention rate")
    out.append("")
    out.append("| # | Brand | Mentions | Rate | Named first | Avg rank when named |")
    out.append("|---:|---|---:|---:|---:|---:|")
    table = []
    for b in brands:
        rs = [r for r in trend_rows if r["brand"] == b["name"]]
        t = sum(int(r["tested"]) for r in rs)
        m = sum(int(r["mentions"]) for r in rs)
        f1 = sum(int(r["named_first"]) for r in rs)
        ranked = [(float(r["avg_rank"]), int(r["mentions"])) for r in rs if r["avg_rank"] != ""]
        avg = round(sum(a * n for a, n in ranked) / sum(n for _, n in ranked), 1) if ranked else ""
        table.append((m, b["name"], t, f1, avg))
    table.sort(reverse=True)
    for i, (m, name, t, f1, avg) in enumerate(table, start=1):
        mark = " (you)" if name in own else ""
        out.append(f"| {i} | {name}{mark} | {m} | {round(100*m/t) if t else 0}% | {f1} | {avg} |")
    out.append("")

    # Queries where no own brand is named anywhere: the opportunity list.
    out.append("## Queries where none of your brands is named on any platform")
    out.append("")
    by_q = defaultdict(list)
    for r in rows:
        if r["own"] == "yes":
            by_q[(r["query_id"], r["query_text"])].append(r["mentioned"] == "yes")
    gaps = [q for q, hits in sorted(by_q.items()) if not any(hits)]
    if gaps:
        for qid, qtext in gaps:
            out.append(f"- Q{qid}: {qtext}")
    else:
        out.append("None.")
    out.append("")

    with open(os.path.join(RUNS, date, "report.md"), "w") as f:
        f.write("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--compare", action="store_true", help="kept for collect.sh compatibility; the report always compares")
    args = ap.parse_args()
    score_run(args.date)
