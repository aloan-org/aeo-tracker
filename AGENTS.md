# Instructions for an AI agent setting this up

You are setting up a weekly AI-search visibility tracker for the person you are working with. Everything you need is in this folder. Work through the steps in order, ask the questions in step 1 before writing any file, and show the person each config file before you run anything that costs money.

## 1. Interview the person

Ask, in one message:

1. Which brands are theirs (name, the spellings people use, the website domain for each).
2. Which competitors they want to be ranked against. Suggest a list from your own knowledge of the category and let them edit it. Eight to fifteen is a good number; an untracked competitor cannot push them down the order, so err on more.
3. What a buyer types when looking for products like theirs. Draft 20 to 40 questions across these kinds and let them edit: non-branded discovery ("best X"), comparison ("alternatives to <market leader>"), occasion or use case, regional (where they sell), retail ("which X brands does <retailer> carry"), and two or three branded ("is <brand> good?"). No locations in the queries ("near me" cannot be measured through these APIs).
4. Which AI platforms to track. Default to ChatGPT, Gemini and Perplexity; add Claude and Grok if they have keys. They need an API key for each (OpenAI, Google AI Studio, Perplexity, Anthropic, xAI). Walk them through creating any they do not have.
5. Where it should run weekly: on their laptop (cron or launchd) or on GitHub Actions (recommended if the repo lives on GitHub; see step 6).

## 2. Write the config

- `brands.json`: replace the placeholder brands. Their brands get `"own": true`. Aliases are whole-word, case-insensitive. Domains without `www.`.
- `queries.json`: replace the placeholder queries. Integer ids starting at 1, a short `category`, the exact text a buyer would type. Never reuse an id later; retire with `"status": "retired"`.
- `.env`: copy `.env.example`, fill in the keys they gave you. Never print the keys back and never commit `.env` (it is gitignored).

Show both JSON files to the person and get a yes before step 3.

## 3. Smoke test (one query, one platform, under a cent)

```bash
bash collect.sh --platform perplexity --queries 1 --date $(date +%Y-%m-%d)
```

Expect `Q1: OK` and `1/1 collected`. If you see `credential rejected`, the key is wrong. Then check that a raw answer landed:

```bash
python3 -c "import json,glob;from extractors import extract_text;f=glob.glob('runs/*/perplexity/q1.json')[-1];print(extract_text(json.load(open(f)),'perplexity')[:600])"
```

Repeat for each platform they enabled.

## 4. First full run

```bash
DATE=$(date +%Y-%m-%d)
bash collect.sh --date "$DATE"
python3 score.py --date "$DATE"
python3 dashboard.py
```

Open `dashboard.html` for them (or `runs/$DATE/report.md`). Walk them through the three numbers (mention rate, rank, cited) and the gap list. Tell them a single run is a baseline, not a result; the trend starts at run two.

## 5. Check the brand matching

False positives and misses are the common first-run problems. Do this once:

- For each own brand, open two answers where it was scored as mentioned and two where it was not, and confirm by eye. `summary.csv` has the per-answer rows; the raw text is in `runs/<date>/<platform>/q<id>.json`.
- If a short alias matches unrelated words (an alias like "ACE" will), remove it.
- If the models spell the brand a way the person did not list, add that spelling as an alias and re-run `score.py` (scoring is free; only collection costs money).

## 6. Schedule it weekly

Pick one day and keep it.

**GitHub Actions** (recommended): rename `.github/workflows/weekly.yml.example` to `weekly.yml`, add the keys as repository secrets with the names in `.env.example`, push. It runs Mondays 13:00 UTC, commits `trend.csv`, the run's `summary.csv` and `report.md`, and the dashboard to `docs/index.html`. Turn on GitHub Pages from `/docs` and the dashboard has a URL.

**Laptop**: a cron line, for example Mondays at 9:

```
0 9 * * 1 cd /path/to/aeo-tracker && bash collect.sh && python3 score.py --date $(date +\%Y-\%m-\%d) && python3 dashboard.py
```

The laptop has to be awake; GitHub Actions does not have that problem.

## 7. Commit

Commit `brands.json`, `queries.json`, `trend.csv`, each run's `summary.csv` and `report.md`, and `dashboard.html` (or `docs/index.html`). The raw `q*.json` answers are gitignored by default; un-ignore them if the person wants the full record (about 20 MB a quarter for 50 queries on five platforms).

## Rules that keep the trend honest

- Do not change the model names in `collect.sh` casually. A model swap is a new baseline; if you do it, record the date in the README.
- Do not delete queries; retire them. Do not renumber.
- Read a one-week move on one query as noise. Two weeks in the same direction is a trend.
- Never commit `.env` or paste a key into a chat, a log or a commit message.
