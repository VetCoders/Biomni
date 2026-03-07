#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3.12}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python3
fi

if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

mkdir -p data

python - <<'PY'
import asyncio
import inspect

try:
    from app.database import init_db
except Exception as exc:
    print(f"[start.sh] Skipping DB init (app.database.init_db unavailable): {exc}")
else:
    try:
        if inspect.iscoroutinefunction(init_db):
            asyncio.run(init_db())
        else:
            maybe = init_db()
            if inspect.isawaitable(maybe):
                asyncio.run(maybe)
        print("[start.sh] Database initialized.")
    except Exception as exc:
        raise SystemExit(f"[start.sh] Database initialization failed: {exc}") from exc
PY

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8129}"

CMD=(uvicorn app.main:app --host "$HOST" --port "$PORT")
if [ "${RELOAD:-0}" = "1" ]; then
  CMD+=(--reload)
fi

exec "${CMD[@]}"
