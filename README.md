# AEO tracker

A small, self-contained way to measure how your brands show up in AI answers. Once a week it asks ChatGPT, Gemini, Claude, Perplexity and Grok the questions your buyers ask, saves every answer, and scores who got named, in what order, and whose web pages the models read to write the answer.

This is a brand-neutral cut of the tracker Aloan has run weekly since March 2026. Everything in it runs from a laptop with Python 3, `jq` and `curl`. There is no database and no hosted service: the raw answers are JSON files on disk, the scores are CSVs, and the weekly report is Markdown.

## What it measures

Three numbers per brand, per platform, per run:

| Metric | Definition | Why it is defined this way |
|---|---|---|
| **Mention rate** | Share of answers that name the brand in the answer prose. | Only prose counts. ChatGPT and Grok paste citation links into the answer text, and matching the brand name against raw text scored a page the model *read* as a brand it *recommended*. That inflated one platform by 11 points before we caught it. `extractors.strip_citation_markup` blanks that markup first. |
| **Rank** | Order in which the brand is first named among the brands you track (1 = named first). | Order of first appearance is stable; "position in the answer" (top, middle, bottom) is not, because it moves whenever a table gets longer. Rank is relative to your brand list, so track competitors generously. |
| **Cited** | At least one of the sources the model retrieved is on one of the brand's domains. | This is the lever you control. The models recommend what they read. If you are not among the sources, the fix is content and distribution, not prompting. ChatGPT's API does not return its full source list, so its citation column is a strict subset; do not read a low ChatGPT citation count as weak coverage. |

The report also lists every query where none of your brands is named on any platform. That list is the week's to-do list.

## How it works

```
queries.json  ─┐
brands.json   ─┤
.env (keys)   ─┴─>  collect.sh  ──>  runs/<date>/<platform>/q<id>.json  (raw answers)
                                            │
                                     score.py
                                            │
                      ┌─────────────────────┼─────────────────────┐
             runs/<date>/summary.csv   runs/<date>/report.md    trend.csv
             (one row per query,       (the week at a glance,   (one row per date,
              platform, brand)          with change vs last run) platform, brand)
```

- `collect.sh` loops over the active queries in `queries.json` and POSTs each one to each platform's API with web search turned on, the way a logged-in consumer gets it. Queries run concurrently per platform, platforms run in parallel, and transient failures (rate limits, 5xx, timeouts) retry in-run. A dead API key aborts that one platform after a single request instead of failing every query.
- `extractors.py` is the only place that knows each platform's response shape. It returns the answer text and the list of source URLs. Add a platform there and nothing else changes.
- `score.py` matches brand names (whole word, case-insensitive, aliases included) against the cleaned answer text, ranks them by first appearance, and checks the source URLs against each brand's domains.
- `resolve-gemini-redirects.py` runs automatically after a Gemini collection. Gemini returns opaque redirect URLs for its sources; this follows each one (a HEAD request, redirects off) to get the real page URL. Tokens expire after a couple of weeks, so it has to run at collection time.

## Setup

1. Install `jq` (`brew install jq` or `apt install jq`). Python 3.9+ is the only other requirement; the scripts use the standard library.
2. `cp .env.example .env` and add the API keys you have. Any platform with an empty key is skipped, so you can start with two platforms and add more later.
3. Edit `brands.json`: your brands with `"own": true`, plus every competitor you want ranked against. Add the spellings the models use as `aliases`, and the domains whose pages should count as yours.
4. Edit `queries.json`: the questions a buyer would type. Twenty to fifty is a good range. Mix non-branded discovery ("best hard cider brands"), comparison ("Angry Orchard alternatives"), occasion, regional and retail questions. Keep a few branded queries ("is Blake's good?") but expect them to score near 100%; they tell you what the models say about you, not whether they find you.
5. Run it:

```bash
bash collect.sh --date 2026-11-03          # all platforms, all active queries
python3 score.py --date 2026-11-03         # writes summary.csv, report.md, trend.csv
```

Partial runs for debugging or a missing platform:

```bash
bash collect.sh --platform perplexity --date 2026-11-03
bash collect.sh --queries 1,2,3 --date 2026-11-03
```

## Running it weekly

Pick a day and keep it. Commit `queries.json`, `brands.json`, `trend.csv` and the dated `report.md` files; the raw `q*.json` answers are gitignored by default (they add up, roughly 20 MB a quarter for 50 queries on five platforms), but keeping them is worth it if you have the space, because every later question about "what did it actually say" is answered from the raw files.

The scoring is only comparable week over week while two things are held constant:

- **The question set.** Retire a query with `"status": "retired"` rather than deleting it; add new ones with new ids. A pooled rate over a changed set is a different number.
- **The models.** The model names are pinned in `collect.sh` (`sonar`, `gpt-6-luna`, `claude-sonnet-5-5`, `gemini-3.6-flash`, `grok-4.6` as of October 2026). When a provider ships a new default, swap it deliberately, note the date, and read the first week after the swap as a new baseline rather than a move. Our worst week-over-week swing came from a model change, not from anything we did.

## Reading the numbers

- Lead with the platforms your buyers actually use. For us that is overwhelmingly ChatGPT, so we weight the pooled rate by each platform's share of our AI referral traffic (check your analytics for referrers from chatgpt.com, gemini.google.com, perplexity.ai, claude.ai). A win on a platform nobody uses is still a win, just a small one.
- A one-week move on a single query is noise. Call something a trend on the second week it repeats.
- When a competitor you did not track shows up in the raw answers on two or more platforms, add it to `brands.json`. Your rank is relative to the list, and an untracked name cannot push you down.
- The citation column tells you which pages the models trust for your category. Open `summary.csv`, filter to the queries you lost, and look at the sources: third-party roundups, retailer pages, review sites and Reddit threads are what gets read. Getting onto those pages moved our numbers more than anything we did on our own site.

## What this does not cover

- **Google AI Overviews and AI Mode.** There is no API for them. Rank-tracking vendors (DataForSEO, SerpApi, Semrush and others) now return the AI Overview block in SERP results, and that is the right source for a consumer brand where Google is the front door. It would slot in as a sixth platform in `extractors.py`.
- **Local and "near me" intent.** Every query here is location-free. The APIs do not take a location the way a phone does, so "hard cider near me" cannot be measured this way; a SERP API with a location parameter can.
- **Shopping surfaces.** ChatGPT and Gemini shopping results come from merchant feeds (Google Merchant Center, Syndigo and similar), not from the web the text answers read. Keep that work separate.
- **Sentiment and accuracy.** This kit counts names and sources. We run a second pass that classifies what each mention says about us (which claims it credits, what drawback it raises) with an LLM judge. It is useful, it is also where most of the cost and all of the subtlety lives, so it is left out of the starter.

## Cost

Last week's run of 49 queries on five platforms cost about $12 in API fees, of which Grok was $7.50 and OpenAI under 10 cents (billed on its half-price flex tier). Twenty queries on three platforms will run a dollar or two a week.

## License

MIT. Use it, change it, no attribution needed.
