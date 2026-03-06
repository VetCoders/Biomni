#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

RELOAD_FLAG=""
if [ "${RELOAD:-}" = "1" ]; then
  RELOAD_FLAG="--reload"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8129 $RELOAD_FLAG
