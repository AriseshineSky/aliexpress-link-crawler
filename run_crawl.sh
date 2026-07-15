#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export TMPDIR="${TMPDIR:-$ROOT/../.tmp}"
export DISPLAY="${DISPLAY:-:0}"

mkdir -p "$TMPDIR" "$ROOT/data"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
  "$ROOT/.venv/bin/playwright" install chromium
fi

# 停掉旧进程，避免 browser profile 被占用导致 TargetClosedError
pkill -f "$ROOT/.venv/bin/python.*alilj.py" 2>/dev/null || true
pkill -f "$ROOT/.venv/bin/python -m aliexpress_spider crawl" 2>/dev/null || true
pkill -f "$ROOT/browser" 2>/dev/null || true
sleep 2
# Clear Chromium singleton locks for default + worker-* profiles
find "$ROOT/browser" -maxdepth 2 \( \
  -name 'SingletonLock' -o -name 'SingletonSocket' -o -name 'SingletonCookie' \
\) -delete 2>/dev/null || true

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -u "$ROOT/alilj.py" "$@"
