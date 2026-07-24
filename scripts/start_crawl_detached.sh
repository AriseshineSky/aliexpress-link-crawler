#!/usr/bin/env bash
# Detached start for AliExpress product-id / category link crawler.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PID_FILE="$ROOT/crawl.pid"
LOG_FILE="$ROOT/crawl.log"
PY="$ROOT/.venv/bin/python"

if [[ -f "$PID_FILE" ]]; then
  old="$(tr -d '[:space:]' <"$PID_FILE" || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    # Only stop if it's our python crawler
    if [[ "$(ps -p "$old" -o args= 2>/dev/null || true)" == *"$PY"* ]]; then
      kill "$old" 2>/dev/null || true
      sleep 2
      kill -9 "$old" 2>/dev/null || true
    fi
  fi
fi

mkdir -p "$ROOT/../.tmp" "$ROOT/data"
find "$ROOT/browser" -maxdepth 2 \( \
  -name SingletonLock -o -name SingletonSocket -o -name SingletonCookie \
\) -delete 2>/dev/null || true

ts="$(date +%Y%m%d%H%M%S)"
if [[ -f "$LOG_FILE" ]]; then
  mv "$LOG_FILE" "$LOG_FILE.bak.$ts"
fi

export DISPLAY="${DISPLAY:-:0}"
export TMPDIR="${TMPDIR:-$ROOT/../.tmp}"
# Do not skip already-crawled category URLs; reclaim done seeds.
export CRAWL_RECLAIM_DONE=1
export CATEGORY_CLAIM_MODE=1
export QUALITY_FILTER=1
export HEADLESS="${HEADLESS:-0}"
# Slower pacing to reduce captcha / punish redirects.
export CRAWL_WORKERS="${CRAWL_WORKERS:-1}"
export REQUEST_DELAY_MS="${REQUEST_DELAY_MS:-2500,4500}"
export GOTO_SETTLE_MS="${GOTO_SETTLE_MS:-2000}"
export ENRICH_CONCURRENCY="${ENRICH_CONCURRENCY:-4}"
export PYTHONUNBUFFERED=1

setsid "$PY" -u "$ROOT/alilj.py" >>"$LOG_FILE" 2>&1 </dev/null &
echo $! >"$PID_FILE"
sleep 5
pid="$(tr -d '[:space:]' <"$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  echo "STARTED pid=$pid log=$LOG_FILE"
  ps -p "$pid" -o pid=,etime=,cmd=
  tail -30 "$LOG_FILE"
  exit 0
fi
echo "FAILED to start; last log:"
tail -50 "$LOG_FILE" || true
exit 1
