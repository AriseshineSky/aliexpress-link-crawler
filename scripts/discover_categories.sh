#!/usr/bin/env bash
# Discover category URLs only (no product listing crawl).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export TMPDIR="${TMPDIR:-$ROOT/../.tmp}"
export DISPLAY="${DISPLAY:-:0}"
mkdir -p "$TMPDIR" "$ROOT/data"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -u "$ROOT/discover_categories.py" "$@"
