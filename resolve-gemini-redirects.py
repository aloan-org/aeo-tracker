#!/usr/bin/env python3
"""Resolve Gemini's grounding-redirect URLs to the real cited page URLs.

Gemini never returns the page it cited. Each groundingChunk carries an opaque
vertexaisearch.cloud.google.com/grounding-api-redirect/<token> URI plus a title
that is only ever a bare domain ("youtube.com"), so extractors.py fell back to
synthesizing https://<title> — every Gemini citation landed on a bare host, and
three chunks pointing at three different pages on one domain collapsed into a
single row.

The redirect itself carries the answer: a HEAD with redirect-following OFF
returns 302 + Location = the real URL. One request per token, and the
destination is never fetched.

Run it at collection time. The tokens do expire, but not immediately, so a
recent run can still be backfilled while its raw files are on disk. Measured
2026-08-11: runs back to 2026-07-26 (16 days) resolve fully; 2026-05-31 (72
days) and older return 404 for every token. The exact cutoff is unknown — the
runs in between were collected in browser shape and carry no redirects.

Writes the map to a top-level "_resolved_uris" key on each result file
({redirect_uri: real_url}), leaving the API payload itself untouched. Rerunning
is safe: already-resolved URIs are skipped, and a URI that fails to resolve is
simply left out, so extractors falls back to the old bare-domain behavior.

Usage:
    resolve-gemini-redirects.py --run-dir runs/2026-08-16/gemini
    resolve-gemini-redirects.py runs/2026-08-16/gemini/q1.json ...
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REDIRECT_HOST = "vertexaisearch.cloud.google.com"
RESOLVED_KEY = "_resolved_uris"
# Cloudflare and friends 403 the default urllib UA — same trap as Apollo/Cal.com.
USER_AGENT = "Mozilla/5.0"
TIMEOUT = 15
WORKERS = 8
ATTEMPTS = 2


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the 302 so we read Location instead of fetching the destination."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def resolve_one(uri):
    """Return the real URL behind a grounding redirect, or None if unresolvable."""
    for attempt in range(ATTEMPTS):
        try:
            req = urllib.request.Request(
                uri, method="HEAD", headers={"User-Agent": USER_AGENT})
            resp = _opener.open(req, timeout=TIMEOUT)
            # A 200 means no redirect was issued — nothing to learn from it.
            location = resp.headers.get("Location")
            resp.close()
            if location:
                return location
            return None
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location")
            if location:
                return location
            # 404 = expired token. Retrying will not help.
            if e.code == 404:
                return None
        except Exception:
            pass
    return None


def chunk_uris(data):
    """Every grounding-redirect URI in one Gemini result file."""
    try:
        chunks = data["candidates"][0]["groundingMetadata"]["groundingChunks"]
    except (KeyError, IndexError, TypeError):
        return []
    uris = []
    for chunk in chunks:
        uri = (chunk.get("web") or {}).get("uri", "")
        if REDIRECT_HOST in uri:
            uris.append(uri)
    return uris


def process_file(path):
    """Resolve one file's redirects in place. Returns (resolved, failed, total)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        # Don't stay silent: these raw files are gitignored, so a corrupt one
        # would otherwise just quietly vanish from every downstream metric.
        print(f"  WARN: could not read {path}: {e}", file=sys.stderr)
        return (0, 0, 0)

    already = data.get(RESOLVED_KEY) or {}
    todo = [u for u in dict.fromkeys(chunk_uris(data)) if u not in already]
    if not todo:
        return (0, 0, len(already))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(resolve_one, todo))

    resolved = {uri: real for uri, real in zip(todo, results) if real}
    if resolved:
        data[RESOLVED_KEY] = {**already, **resolved}
        # Atomic replace: this file is the ONLY copy of the raw response (raw
        # is gitignored), and a crash mid-dump would truncate it irrecoverably.
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    return (len(resolved), len(todo) - len(resolved), len(already) + len(resolved))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="Gemini q<id>.json result files")
    ap.add_argument("--run-dir", type=Path,
                    help="Directory of Gemini results; processes every q*.json in it")
    args = ap.parse_args()

    paths = list(args.files)
    if args.run_dir:
        paths.extend(sorted(args.run_dir.glob("q*.json")))
        if not paths:
            # A run dir with nothing in it is a collection that failed upstream
            # (bad key, dead endpoint), not a misuse of this script. Say so and
            # exit clean so audit.sh's collector summary is the line that gets read.
            print(f"no q*.json under {args.run_dir}: nothing to resolve")
            return
    if not paths:
        ap.error("pass result files or --run-dir")

    total_ok = total_fail = 0
    for path in paths:
        ok, fail, _ = process_file(path)
        total_ok += ok
        total_fail += fail

    print(f"  gemini redirects: {total_ok} resolved, {total_fail} unresolved "
          f"across {len(paths)} file(s)")
    if total_fail and not total_ok:
        print("  (all failed — tokens likely expired; resolve at collection time)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
