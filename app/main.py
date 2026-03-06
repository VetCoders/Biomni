from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "static"
ENV_FILE = PROJECT_ROOT / ".env"


def _api_url() -> str:
    return os.getenv("BIOMNI_API_URL", "https://api.libraxis.cloud").rstrip("/")


def _api_key() -> str:
    return os.getenv("BIOMNI_API_KEY", os.getenv("LIBRAXIS_API_KEY", ""))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=True)
    yield


app = FastAPI(title="Biomni Portal", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API: health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, Any]:
    key = _api_key()
    has_key = bool(key)
    upstream_ok = False
    if has_key:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{_api_url()}/v1/biomni/health",
                    headers={"Authorization": f"Bearer {key}"},
                )
                upstream_ok = r.status_code == 200
        except Exception:
            pass
    return {
        "status": "ok",
        "service": "biomni-portal",
        "has_api_key": has_key,
        "upstream": "ok" if upstream_ok else "unreachable",
    }


# ---------------------------------------------------------------------------
# API: proxy stream
# ---------------------------------------------------------------------------

@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt or len(prompt) < 2:
        return JSONResponse({"error": "Prompt too short"}, status_code=400)

    key = _api_key()
    url = f"{_api_url()}/v1/biomni/stream"

    async def _proxy():
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=120)) as c:
            async with c.stream(
                "POST",
                url,
                json={"prompt": prompt},
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "text/event-stream",
                },
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(_proxy(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# API: proxy query (non-streaming fallback)
# ---------------------------------------------------------------------------

@app.post("/api/chat/query")
async def chat_query(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt or len(prompt) < 2:
        return JSONResponse({"error": "Prompt too short"}, status_code=400)

    key = _api_key()
    url = f"{_api_url()}/v1/biomni/query"

    async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=120)) as c:
        resp = await c.post(
            url,
            json={"prompt": prompt},
            headers={"Authorization": f"Bearer {key}"},
        )
        return JSONResponse(resp.json(), status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Static files (SPA)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
