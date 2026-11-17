#!/usr/bin/env bash
# One snapshot of calendar, COT and retail sentiment (see fxnext.news.archive).
# Meant to run every 30 minutes from Windows Task Scheduler through MSYS2 bash.
# Output folder: $FXNEXT_NEWS_DIR, default ~/FxAgentNext/data/news.
set -u
cd "$(dirname "$0")/.." || exit 1
if ! command -v uv >/dev/null 2>&1 && [ -n "${USERPROFILE:-}" ]; then
  PATH="$PATH:$(cygpath -u "$USERPROFILE")/.local/bin"
fi
out="${FXNEXT_NEWS_DIR:-$HOME/FxAgentNext/data/news}"
mkdir -p "$out"
{
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  uv run --no-sync fxnext news-archive --out "$out"
} >> "$out/archive.log" 2>&1
