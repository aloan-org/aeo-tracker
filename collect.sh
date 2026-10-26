#!/bin/bash
# AEO collector: runs every query in queries.json against the AI platforms and stores the raw answers
# Usage:
#   ./collect.sh                           # all platforms, all queries, today's date
#   ./collect.sh --platform perplexity     # one platform only
#   ./collect.sh --queries 1,2,13,14       # specific query IDs only
#   ./collect.sh --date 2026-03-29         # override date (for backfills)
#   ./collect.sh --analyze                 # run score.py after collection
# Env knobs: AEO_CONCURRENCY=1 (per-query, sequential within a platform),
#            AEO_PLATFORM_PARALLEL=0 (platforms one at a time, pre-2026-08-14 behavior)

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Load API keys
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  source "$REPO_ROOT/.env"
  set +a
else
  echo "ERROR: $REPO_ROOT/.env not found"
  exit 1
fi

# Default missing keys to empty (prevents unbound variable errors)
: "${PERPLEXITY_API_KEY:=}" "${OPENAI_API_KEY:=}" "${ANTHROPIC_API_KEY:=}" "${GEMINI_API_KEY:=}" "${XAI_API_KEY:=}"

# Pre-flight: report which keys are available
echo "API keys:"
for key in PERPLEXITY_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY GEMINI_API_KEY XAI_API_KEY; do
  if [ -n "${!key}" ]; then
    echo "  $key: set"
  else
    echo "  $key: MISSING"
  fi
done
echo ""

# Defaults
DATE=$(date +%Y-%m-%d)
PLATFORMS="perplexity openai anthropic gemini grok"
QUERY_FILTER=""
RUN_ANALYZE=false

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --platform)  PLATFORMS="$2"; shift 2 ;;
    --queries)   QUERY_FILTER="$2"; shift 2 ;;
    --date)      DATE="$2"; shift 2 ;;
    --analyze)   RUN_ANALYZE=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--platform NAME] [--queries 1,2,3] [--date YYYY-MM-DD] [--analyze]"
      echo "Platforms: perplexity, openai, anthropic, gemini, grok"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

QUERIES_FILE="$SCRIPT_DIR/queries.json"
if [ ! -f "$QUERIES_FILE" ]; then
  echo "ERROR: $QUERIES_FILE not found"
  exit 1
fi

# Check jq
if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required. Install with: apt install jq / brew install jq"
  exit 1
fi

# Build query ID list
# Skip queries with status="retired"; missing status defaults to active.
if [ -n "$QUERY_FILTER" ]; then
  QUERY_IDS=$(echo "$QUERY_FILTER" | tr ',' '\n')
else
  QUERY_IDS=$(jq -r '.queries[] | select((.status // "active") != "retired") | .id' "$QUERIES_FILE")
fi

total_queries=$(echo "$QUERY_IDS" | wc -l | tr -d ' ')
echo "=== AEO Audit: $DATE ==="
echo "Platforms: $PLATFORMS"
echo "Queries: $total_queries"
echo ""

# --- Collection backend per platform (sources.json) ---
# 'api'     -> the curl runners below.
# 'browser' -> the Playwright collector (chatgpt-web-collect.js, run via the
#              Playwright MCP) drives the real consumer product and writes the
#              same runs/<date>/<platform>/q*.json files; audit.sh skips it here.
SOURCES_FILE="$SCRIPT_DIR/sources.json"
backend_for() {
  if [ -f "$SOURCES_FILE" ]; then
    jq -r --arg p "$1" '.[$p] // "api"' "$SOURCES_FILE" 2>/dev/null || echo "api"
  else
    echo "api"
  fi
}
# Provenance: snapshot which backend produced each platform this cycle, so the
# trend stays interpretable across backend switches (a switch is a rebaseline).
# Merged under a lock, not overwritten: run-weekly-audit.sh and backfill.sh run
# several single-platform audit.sh processes concurrently, and a plain
# overwrite left only the last writer's platform in the record (every weekly
# snapshot 2026-06-21..2026-08-09 except two lost entries this way).
mkdir -p "$SCRIPT_DIR/runs/$DATE"
sources_snapshot="{}"
for platform in $PLATFORMS; do
  sources_snapshot=$(echo "$sources_snapshot" | jq --arg p "$platform" --arg b "$(backend_for "$platform")" '. + {($p): $b}')
done
snapshot_file="$SCRIPT_DIR/runs/$DATE/sources.json"
lock_dir="$snapshot_file.lock"
lock_tries=0
until mkdir "$lock_dir" 2>/dev/null; do
  lock_tries=$((lock_tries+1))
  if [ "$lock_tries" -ge 50 ]; then break; fi  # stale lock from a killed run: proceed after ~5s
  sleep 0.1
done
existing_snapshot="{}"
if [ -s "$snapshot_file" ] && jq empty "$snapshot_file" 2>/dev/null; then
  existing_snapshot=$(cat "$snapshot_file")
fi
echo "$existing_snapshot" | jq --argjson add "$sources_snapshot" '. + $add' > "$snapshot_file.tmp.$$" \
  && mv "$snapshot_file.tmp.$$" "$snapshot_file"
rmdir "$lock_dir" 2>/dev/null || true

# --- Platform runners ---

# POST with retries on the transient failure modes (429 rate limit, 5xx, and
# timeout/network error = 000), linear backoff. This is the same loop that has
# run on openai's flex tier since 2026-08-02, now shared by every platform: a
# transient failure used to leave a gap for backfill.sh, now it retries in-run.
# Non-transient statuses (400/401/...) return immediately; process_query
# surfaces the error body. Requests themselves are unchanged.
post_with_retry() {
  local outfile=$1 max_time=$2
  shift 2
  local attempt http_code
  for attempt in 1 2 3; do
    http_code=$(curl -s -o "$outfile" -w "%{http_code}" --max-time "$max_time" "$@") || http_code=000
    case $http_code in
      429|500|502|503|000) [ "$attempt" -lt 3 ] && sleep $((attempt * 20)) ;;
      *) return 0 ;;
    esac
  done
  return 0
}

run_perplexity() {
  local qid=$1 qtext=$2 outfile=$3
  if [ -z "$PERPLEXITY_API_KEY" ]; then
    echo "  SKIP (no PERPLEXITY_API_KEY)"
    return
  fi
  post_with_retry "$outfile" 60 https://api.perplexity.ai/chat/completions \
    -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$qtext" '{model:"sonar",messages:[{role:"user",content:$q}]}')"
}

run_openai() {
  local qid=$1 qtext=$2 outfile=$3
  if [ -z "$OPENAI_API_KEY" ]; then
    echo "  SKIP (no OPENAI_API_KEY)"
    return
  fi
  # Flex service tier: 50% off, same pinned model — billing/latency tier only,
  # NOT an instrument change (see RUNBOOK "Model config & baselines").
  # OPENAI_SERVICE_TIER=default falls back to standard if flex queues badly.
  # Flex latency is variable, so allow a long timeout and retry the two flex
  # failure modes (429 resource_unavailable, transient 5xx) with backoff.
  local tier="${OPENAI_SERVICE_TIER:-flex}"
  # Search is OPTIONAL as of 2026-08-09: the model decides per query, like the real
  # product. This IS an instrument change and rebaselines the OpenAI column — the
  # 2026-06-14 calibration measured forced 59% vs auto 34% on the same queries.
  # OPENAI_TOOL_CHOICE=required reruns the old forced instrument for comparison.
  local tool_choice="${OPENAI_TOOL_CHOICE:-auto}"
  post_with_retry "$outfile" 900 https://api.openai.com/v1/responses \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$qtext" --arg tier "$tier" --arg tc "$tool_choice" '{
      model: "gpt-6-luna",
      service_tier: $tier,
      tools: [{type: "web_search"}],
      tool_choice: $tc,
      input: $q
    }')"
}

run_anthropic() {
  local qid=$1 qtext=$2 outfile=$3
  if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "  SKIP (no ANTHROPIC_API_KEY)"
    return
  fi
  # claude-sonnet-5-5 since 2026-10-11 (was claude-haiku-4-5 with forced search).
  # Sonnet 5.5 rejects a forced tool_choice, so search is offered and the model
  # decides per query, as on claude.ai and as OpenAI has done since 2026-08-09.
  # An org-level key on an account with workspaces must name the workspace
  # (2026-09-20: "This API key is not scoped to a workspace"). Set
  # ANTHROPIC_WORKSPACE_ID in .env, or use a workspace-scoped key and leave
  # it unset.
  post_with_retry "$outfile" 120 https://api.anthropic.com/v1/messages \
    -H "x-api-key: $ANTHROPIC_API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    ${ANTHROPIC_WORKSPACE_ID:+-H "anthropic-workspace-id: $ANTHROPIC_WORKSPACE_ID"} \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$qtext" '{
      model: "claude-sonnet-5-5",
      max_tokens: 4096,
      tools: [{type: "web_search_20250305", name: "web_search", max_uses: 3}],
      messages: [{role: "user", content: $q}]
    }')"
}

run_gemini() {
  local qid=$1 qtext=$2 outfile=$3
  if [ -z "$GEMINI_API_KEY" ]; then
    echo "  SKIP (no GEMINI_API_KEY)"
    return
  fi
  # gemini-3.6-flash since 2026-10-11: Google began serving 3.6 to requests for
  # gemini-3.5-flash with no dated 3.5 ID left to pin (RUNBOOK Baseline history).
  post_with_retry "$outfile" 240 "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=$GEMINI_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$qtext" '{
      contents: [{parts: [{text: $q}]}],
      tools: [{google_search: {}}]
    }')"
}

run_grok() {
  local qid=$1 qtext=$2 outfile=$3
  if [ -z "$XAI_API_KEY" ]; then
    echo "  SKIP (no XAI_API_KEY)"
    return
  fi
  post_with_retry "$outfile" 240 https://api.x.ai/v1/responses \
    -H "Authorization: Bearer $XAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$qtext" '{
      model: "grok-4.6",
      tools: [{type: "web_search"}],
      input: [{role: "user", content: $q}]
    }')"
}

# --- Per-query worker: call the platform, validate, report one status line ---

process_query() {
  local platform=$1 qid=$2 qtext=$3 outfile=$4

  case $platform in
    perplexity) run_perplexity "$qid" "$qtext" "$outfile" ;;
    openai)     run_openai "$qid" "$qtext" "$outfile" ;;
    anthropic)  run_anthropic "$qid" "$qtext" "$outfile" ;;
    gemini)     run_gemini "$qid" "$qtext" "$outfile" ;;
    grok)       run_grok "$qid" "$qtext" "$outfile" ;;
    *) echo "  Q$qid: unknown platform $platform"; return ;;
  esac

  if [ -s "$outfile" ]; then
    # Validate response is valid JSON (catches partial writes from timeouts)
    if ! jq empty "$outfile" &>/dev/null; then
      echo "  Q$qid: INVALID JSON (likely timeout / partial write)"
      rm -f "$outfile"
    # Check for API errors (handles both {"error":"str"} and {"error":{"message":"str"}})
    elif jq -e '.error' "$outfile" &>/dev/null; then
      local error
      error=$(jq -r 'if (.error | type) == "string" then .error else (.error.message // .error.type // "unknown") end' "$outfile")
      echo "  Q$qid: ERROR: $error"
      rm -f "$outfile"
    else
      echo "  Q$qid: OK"
    fi
  else
    echo "  Q$qid: EMPTY (no response / timeout)"
    rm -f "$outfile"
  fi
}

# Queries within a platform run concurrently (queries are independent; results
# land in separate q<id>.json files). Concurrency + launch stagger are tuned
# per provider rate limits; AEO_CONCURRENCY=1 restores the old sequential run.
concurrency_for() {
  if [ -n "${AEO_CONCURRENCY:-}" ]; then echo "$AEO_CONCURRENCY"; return; fi
  case $1 in
    openai)    echo 6 ;;
    anthropic) echo 2 ;;  # tightest rate limits (was sleep 3 sequential)
    *)         echo 4 ;;
  esac
}
stagger_for() {
  case $1 in
    anthropic) echo 1.5 ;;
    *)         echo 0.3 ;;
  esac
}

# One platform's full collection: query loop under the tuned concurrency cap,
# then platform-specific post-processing. Runs either inline (sequential mode)
# or in a background subshell (parallel mode) — `jobs -rp` in the throttle
# counts only the calling shell's own jobs, so the per-platform cap holds
# either way.
collect_platform() {
  local platform=$1
  echo "--- $platform ---"

  if [ "$(backend_for "$platform")" = "browser" ]; then
    echo "  backend=browser -> collected by chatgpt-web-collect.js (Playwright MCP); skipping API"
    echo ""
    return 0
  fi

  local OUTDIR="$SCRIPT_DIR/runs/$DATE/$platform"
  mkdir -p "$OUTDIR"

  local max_jobs stagger launched qid qtext outfile collected
  max_jobs=$(concurrency_for "$platform")
  stagger=$(stagger_for "$platform")
  launched=0
  for qid in $QUERY_IDS; do
    # Fail fast on a dead credential. The first query runs inline (not in the
    # background); if it comes back with an auth error the remaining 43 would
    # all fail the same way, so stop here with one line that names the cause
    # instead of 44 identical ERROR lines and a "0/44 collected" tail. On
    # 2026-09-05 Gemini did exactly that with a revoked key. Rate limits and
    # 5xx are NOT auth errors and keep the normal retry path. A depleted
    # prepaid balance ("credit balance is too low", 2026-09-20 on Anthropic)
    # is a 400, not a 401, but fails every query the same way, so it aborts
    # too; the fix is billing, not the key.
    if [ "$launched" -eq 0 ]; then
      case $qid in ''|*[!0-9]*) continue ;; esac
      qtext=$(jq -r --argjson id "$qid" '.queries[] | select(.id == $id) | .text' "$QUERIES_FILE")
      [ -z "$qtext" ] || [ "$qtext" = "null" ] && continue
      outfile="$OUTDIR/q${qid}.json"
      if [ ! -s "$outfile" ]; then
        local probe
        probe=$(process_query "$platform" "$qid" "$qtext" "$outfile" 2>&1)
        echo "$probe"
        if echo "$probe" | grep -qiE 'api key not valid|invalid api key|invalid_api_key|incorrect api key|authentication|unauthorized|permission denied|invalid x-api-key|401|403|credit balance is too low|insufficient_quota|billing_not_active|not scoped to a workspace'; then
          echo "  $platform: ABORTED after Q$qid - credential rejected or balance depleted; fix the key or billing and re-run this platform"
          echo ""
          return 0
        fi
        launched=1
        continue
      fi
    fi
    # Guard --queries input: a stray comma / non-numeric id would make
    # `jq --argjson` fail and set -e kill the whole run.
    case $qid in ''|*[!0-9]*) echo "  Q$qid: SKIP (bad id)"; continue ;; esac
    qtext=$(jq -r --argjson id "$qid" '.queries[] | select(.id == $id) | .text' "$QUERIES_FILE")
    if [ -z "$qtext" ] || [ "$qtext" = "null" ]; then
      echo "  Q$qid: SKIP (not found in queries.json)"
      continue
    fi

    outfile="$OUTDIR/q${qid}.json"

    # Skip if already collected — but only if it parses. A truncated file
    # (interrupted run, killed mid-write) would otherwise be kept forever:
    # every downstream reader catches the parse error and silently drops
    # the query from its metrics.
    if [ -s "$outfile" ]; then
      if jq empty "$outfile" &>/dev/null; then
        echo "  Q$qid: skip (exists)"
        continue
      fi
      echo "  Q$qid: recollect (existing file is invalid JSON)"
      rm -f "$outfile"
    fi

    # Throttle: wait for a free slot (bash 3.2-safe; no wait -n on macOS)
    while [ "$(jobs -rp | wc -l)" -ge "$max_jobs" ]; do
      sleep 0.5
    done

    process_query "$platform" "$qid" "$qtext" "$outfile" &
    launched=$((launched+1))
    sleep "$stagger"
  done

  wait

  # Gemini cites via opaque grounding-redirect URLs and reports only a bare
  # domain as the title, so without this every Gemini citation lands on a
  # host-only URL. The redirect tokens expire (weeks, not hours), so resolve
  # while the run is fresh. Idempotent and non-fatal.
  if [ "$platform" = "gemini" ]; then
    python3 "$SCRIPT_DIR/resolve-gemini-redirects.py" --run-dir "$OUTDIR" || \
      echo "  WARN: redirect resolution failed; Gemini citations stay bare-host"
  fi

  # find, not `ls q*.json`: with zero collected files (e.g. a missing API key)
  # the unmatched glob made ls fail and set -e killed the collector before the
  # summary line — the keyless-platform case died silently for months.
  collected=$(find "$OUTDIR" -name 'q*.json' 2>/dev/null | wc -l | tr -d ' ')
  echo "  $platform: $launched launched, $collected/$total_queries collected"
  echo ""
}

# --- Main loop ---
# Platforms run concurrently by default: rate limits are per provider, so the
# per-platform caps above see no extra load and wall clock drops from the sum
# of the platforms to the slowest one. Collection order changes, the requests
# do not (NOT an instrument change — RUNBOOK "Model config & baselines").
# AEO_PLATFORM_PARALLEL=0 restores the sequential platform loop; a
# single-platform invocation (run-weekly-audit.sh's and backfill.sh's unit of
# work) always runs inline, exactly as before.

platform_count=$(echo $PLATFORMS | wc -w | tr -d ' ')
if [ "${AEO_PLATFORM_PARALLEL:-1}" = "0" ] || [ "$platform_count" -le 1 ]; then
  for platform in $PLATFORMS; do
    collect_platform "$platform"
  done
else
  pids=()
  platform_list=()
  for platform in $PLATFORMS; do
    ( collect_platform "$platform" ) > "$SCRIPT_DIR/runs/$DATE/collect-$platform.log" 2>&1 &
    pids+=("$!")
    platform_list+=("$platform")
    echo "collecting $platform in background (log: runs/$DATE/collect-$platform.log)"
  done
  echo ""
  # Wait in launch order; replay each platform's log as it completes so the
  # output reads like the sequential run's. A collector that died (its subshell
  # inherits set -e) fails the whole run, matching what the sequential loop
  # would have done — don't let a dead platform pass silently.
  failed_platforms=""
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || failed_platforms="$failed_platforms ${platform_list[$i]}"
    cat "$SCRIPT_DIR/runs/$DATE/collect-${platform_list[$i]}.log"
  done
  if [ -n "$failed_platforms" ]; then
    echo "ERROR: collector(s) exited non-zero:$failed_platforms" >&2
    exit 1
  fi
fi

echo "=== Collection complete ==="
echo "Results: $SCRIPT_DIR/runs/$DATE/"

# Run analysis if requested
if [ "$RUN_ANALYZE" = true ]; then
  echo ""
  echo "=== Running analysis ==="
  python3 "$SCRIPT_DIR/score.py" --date "$DATE"
  echo ""
  echo "=== Scoring ==="
fi
