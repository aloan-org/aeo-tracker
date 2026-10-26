#!/usr/bin/env python3
"""Canonical per-platform extraction from raw AEO API result JSON.

Single source of truth for turning a raw API response into (a) the LLM's
response text and (b) its citation/source URLs. Both `score.py` (which writes
trend.csv) and any dashboard you build import from
here so the audit pipeline and the dashboard can never drift apart.

Add a new platform by extending KNOWN_PLATFORMS and the branches below — in one
place, not two.
"""

import re
from urllib.parse import urlparse

KNOWN_PLATFORMS = ("perplexity", "openai", "anthropic", "gemini", "grok")

_URL_IN_TEXT = re.compile(r'https?://[^\s\)]+')

# Rebaseline boundary: the first run whose Grok column counts retrieved sources
# rather than only inline citations (see extract_citation_urls). Pinned to a date
# instead of "wherever the raw happens to support it", because raw is gitignored
# and support is not contiguous — 2026-06-07..06-28 have no raw left and
# 2026-07-05..07-19 were collected in browser shape, so applying it opportunistically
# gives an old/new/old/new series with two artificial breaks instead of one.
# 2026-07-26 is the start of the contiguous recent block that the raw does support.
# Runs before it keep the inline-only basis permanently; that is also what a fresh
# clone reproduces, since it falls back to the committed citations.csv.
GROK_SOURCES_FROM = "2026-07-26"

# --- Citation markup inside the answer text -------------------------------
#
# Some platforms render their citations INTO the answer body, so the response
# text is not clean prose:
#
#   grok    "...at the analyst level.[[3]](https://yourbrand.com/page/...)"
#   openai  "...and credit memo assembly. ([yourbrand.com](https://yourbrand.com/...))"
#
# gemini / perplexity / anthropic keep sources in structured fields instead, so
# their text is unaffected. Matching a brand name against the raw text therefore
# scores a citation as a mention on two platforms and not on the other three —
# it counted a page the model *read* as a vendor the model *named*. Measured
# 2026-08-16 that inflated Grok's organic rate by 11 points and OpenAI's by 2,
# and credited LoanPro with presence in 7 Grok responses that never name it (it
# publishes a "best LOS software" roundup Grok reads as a source).
#
# strip_citation_markup() blanks those spans before any name matching. It is
# offset- and line-preserving (NUL fill, same length, newlines kept) so the
# character-offset rank math and line-index position bucketing in score.py
# stay valid against the masked text.
#
# Deliberately NOT masked: a markdown link with a descriptive label, e.g.
# "[Your brand page](https://yourbrand.com/...)". Only the URL is
# blanked there — the label is prose the reader sees, so it stays a mention.
_URL_SPAN = re.compile(r'(?:https?://|www\.)[^\s\)\]\}<>"\']+')
_MD_LINK = re.compile(r'\[([^\]\n]{0,200})\]\((?:https?://|www\.)[^\s\)]+\)')
_BARE_DOMAIN_LABEL = re.compile(r'^[\w.-]+\.(?:ai|com|io|net|org|co|us|gov|edu)$', re.I)


def strip_citation_markup(text):
    """Blank citation markup in an answer body, preserving offsets and lines.

    Masks (a) every URL span and (b) markdown link labels that are a bare domain
    (the inline source chip OpenAI renders). Replaced with NUL so no name pattern
    can match inside, while len(masked) == len(text) and every '\\n' stays put.
    """
    if not text:
        return text or ""
    spans = [m.span() for m in _URL_SPAN.finditer(text)]
    for m in _MD_LINK.finditer(text):
        if _BARE_DOMAIN_LABEL.match(m.group(1).strip()):
            spans.append(m.span(1))
    if not spans:
        return text
    out = list(text)
    for start, end in spans:
        for i in range(start, end):
            if out[i] != "\n":
                out[i] = "\x00"
    return "".join(out)


def extract_text(data, platform):
    """Return the LLM response text from a raw API result ('' if none).

    Text blocks/parts are joined with newlines so paragraph boundaries survive.

    Backend-agnostic: a browser-collected file (the Playwright collector driving
    the real consumer product) carries its text/citations directly under a
    "_source":"browser" marker. The shape is the same for every platform, so a
    run is identical downstream whether it was collected via API or browser — that
    is what lets `sources.json` swap a platform's backend without touching score.py
    or the dashboard.
    """
    if isinstance(data, dict) and data.get("_source") == "browser":
        return data.get("content", "") or ""
    if platform == "perplexity":
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""
    if platform in ("openai", "grok"):
        parts = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        parts.append(block.get("text", ""))
        return "\n".join(parts)
    if platform == "anthropic":
        parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    if platform == "gemini":
        parts = []
        try:
            for part in data["candidates"][0]["content"]["parts"]:
                parts.append(part.get("text", ""))
        except (KeyError, IndexError):
            pass
        return "\n".join(parts)
    return ""


def extract_citation_urls(data, platform, date=None):
    """Return the deduped list of citation/source URLs from a raw API result.

    `date` is the run date, used only to honour the GROK_SOURCES_FROM rebaseline
    boundary. Omitting it means "current run" and applies the current definition,
    so a new collection always gets today's semantics.

    **This returns SOURCES — every URL the model retrieved for the answer — not
    only the ones it cites.** The industry draws the distinction explicitly
    (Peec: "sources" = all URLs accessed during generation, "citations" = the
    subset referenced in the response text). extract_cited_urls() returns that
    subset; since 2026-10-11 the dashboard ranks and shares on it and shows
    sources alongside as "retrieved". Measured on the 2026-10-11 run:

      perplexity  490 sources / 350 cited   [n] markers reference ~70% of the list
      gemini      426 / 426                 identical (groundingSupports attaches
                                            every chunk it retrieves)
      anthropic   517 / 335                 differs
      grok       1939 / 299                 differs
      openai        - / 218                 sources unavailable, see below

    **OpenAI cannot report sources.** Its `web_search_call.action` records the
    query it searched for, never the result set (only `open_page` actions carry
    a URL), so inline `url_citation` annotations are all that survive. On the
    sources basis its column is a strict subset of the other four; on the cited
    basis it is like-for-like.

    Gemini returns opaque redirect URLs (vertexaisearch.cloud.google.com) and a
    chunk `title` that is only ever a bare domain. resolve-gemini-redirects.py
    resolves those redirects at collection time into `_resolved_uris`, which we
    prefer; runs collected before that existed (and any token that failed to
    resolve) fall back to synthesizing https://<title>, which is bare-host only.
    Anthropic carries sources in web_search_tool_result blocks plus inline URLs
    in the text.
    """
    if isinstance(data, dict) and data.get("_source") == "browser":
        return sorted(set(data.get("citations", []) or []))
    urls = []
    if platform == "perplexity":
        urls.extend(data.get("citations", []))
        for sr in data.get("search_results", []):
            if sr.get("url"):
                urls.append(sr["url"])
    elif platform in ("openai", "grok"):
        want_sources = date is None or date >= GROK_SOURCES_FROM
        for item in data.get("output", []):
            # Grok's web_search tool reports what it retrieved under
            # action.sources; OpenAI never does (this loop is a no-op there).
            # Unioned with the inline annotations rather than replacing them:
            # grok runs before 2026-05-17 predate action.sources entirely and
            # carry annotations only, so replacing would zero out seven runs.
            if want_sources:
                for src in (item.get("action") or {}).get("sources", []) or []:
                    if isinstance(src, dict) and src.get("url"):
                        urls.append(src["url"])
            if item.get("type") == "message":
                for block in item.get("content", []):
                    for ann in block.get("annotations", []):
                        if ann.get("type") == "url_citation" and ann.get("url"):
                            urls.append(ann["url"])
    elif platform == "gemini":
        resolved = data.get("_resolved_uris") or {}
        for chunk in _gemini_chunks(data):
            url = _gemini_chunk_url(chunk, resolved)
            if url:
                urls.append(url)
    elif platform == "anthropic":
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                for c in (block.get("citations") or []):
                    if c.get("url"):
                        urls.append(c["url"])
                urls.extend(_URL_IN_TEXT.findall(block.get("text", "")))
            elif btype == "web_search_tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("url"):
                            urls.append(item["url"])

    # Sorted (not just set) so downstream Counter.most_common() tie-breaks are
    # stable — the dashboard then regenerates byte-for-byte instead of reshuffling
    # equal-count rows on every run.
    return sorted(set(urls))


def _gemini_chunks(data):
    try:
        return data["candidates"][0].get("groundingMetadata", {}).get("groundingChunks", []) or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return []


def _gemini_chunk_url(chunk, resolved):
    web = chunk.get("web", {})
    title = web.get("title", "")
    uri = web.get("uri", "")
    if resolved.get(uri):
        return resolved[uri]
    if title and not title.startswith("http"):
        # title is a domain like "hesfintech.com" — synthesize a URL
        return f"https://{title}"
    if uri and "vertexaisearch" not in uri:
        return uri
    return None


_PERPLEXITY_MARKER = re.compile(r'\[(\d+)\]')


def extract_cited_urls(data, platform, date=None):
    """Return the subset of extract_citation_urls() the answer actually cites.

    A source is any URL the model retrieved; a citation is one the answer
    attributes something to. Per platform:

      perplexity  the [n] markers in the text, indexing `citations` (1-based)
      anthropic   text-block `citations` plus URLs written into the text; the
                  web_search_tool_result list is what it retrieved
      grok        inline url_citation annotations; action.sources is retrieval
      openai      inline url_citation annotations, which is all it reports, so
                  cited == sources
      gemini      chunks referenced by groundingSupports; in practice every
                  chunk is referenced, so cited == sources

    Returns None when the shape cannot tell the two apart (browser-collected
    files keep one flat list), so callers record "unknown", never "not cited".
    Always a subset of extract_citation_urls(data, platform, date).
    """
    if isinstance(data, dict) and data.get("_source") == "browser":
        return None
    sources = set(extract_citation_urls(data, platform, date))
    cited = set()
    if platform == "perplexity":
        listed = data.get("citations", []) or []
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        for n in _PERPLEXITY_MARKER.findall(text):
            i = int(n) - 1
            if 0 <= i < len(listed):
                cited.add(listed[i])
    elif platform in ("openai", "grok"):
        for item in data.get("output", []):
            if item.get("type") == "message":
                for block in item.get("content", []):
                    for ann in block.get("annotations", []):
                        if ann.get("type") == "url_citation" and ann.get("url"):
                            cited.add(ann["url"])
    elif platform == "gemini":
        resolved = data.get("_resolved_uris") or {}
        chunks = _gemini_chunks(data)
        try:
            supports = data["candidates"][0].get("groundingMetadata", {}).get("groundingSupports", []) or []
        except (KeyError, IndexError, TypeError, AttributeError):
            supports = []
        for s in supports:
            for i in s.get("groundingChunkIndices", []) or []:
                if 0 <= i < len(chunks):
                    url = _gemini_chunk_url(chunks[i], resolved)
                    if url:
                        cited.add(url)
    elif platform == "anthropic":
        for block in data.get("content", []):
            if block.get("type") == "text":
                for c in (block.get("citations") or []):
                    if c.get("url"):
                        cited.add(c["url"])
                cited.update(_URL_IN_TEXT.findall(block.get("text", "")))
    return sorted(cited & sources)


def url_domain(url):
    """Normalize a URL to its bare domain (no scheme, no www, lowercased)."""
    return urlparse(url).netloc.replace("www.", "").lower()
