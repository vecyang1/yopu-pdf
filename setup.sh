#!/usr/bin/env bash
# One-time setup: local venv + playwright.
# Uses the Google Chrome already installed on this Mac, so no browser download.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
if [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "Google Chrome not found — installing Playwright's chromium instead..."
  ./.venv/bin/playwright install chromium
fi
echo "ok — run ./yopu-pdf <url>"
