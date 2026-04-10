from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.auth import JWTError, decode_access_token, get_current_user
from app.auth import router as auth_router
from app.database import (
    add_message,
    create_conversation,
    delete_conversation_for_user,
    ensure_share_token_for_conversation,
    get_conversation_by_id,
    get_conversation_with_messages_for_user,
    get_shared_conversation,
    get_user_by_id,
    init_db,
    list_conversations_for_user,
)
from app.models import (
    ChatQueryRequest,
    ChatQueryResponse,
    ConversationDetailResponse,
    ConversationHistoryTurn,
    ConversationSummaryResponse,
    HealthResponse,
    MessageResponse,
    PublicCapabilitySummary,
    PublicOverviewResponse,
    PublicReadinessCheck,
    PublicWorkflowCard,
    ShareTokenResponse,
    StreamMessage,
    ToolInfo,
    ToolsResponse,
)
from app.transcribe import (
    MAX_AUDIO_FILE_BYTES,
    stream_transcription_ndjson,
    transcribe_audio_file,
    transcription_available,
    transcription_provider_names,
)

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Simple in-memory token-bucket rate limiter per user."""

    def __init__(self, max_calls: int = 30, period_seconds: int = 60) -> None:
        self._max = max_calls
        self._period = period_seconds
        self._buckets: dict[str, list[float]] = {}
        self._last_eviction: float = 0.0

    def check(self, user_id: str) -> bool:
        now = time.time()
        # Evict stale user buckets every 5 minutes to prevent unbounded dict growth.
        if now - self._last_eviction > 300:
            stale = [uid for uid, ts in self._buckets.items() if not ts or now - ts[-1] > self._period]
            for uid in stale:
                del self._buckets[uid]
            self._last_eviction = now
        bucket = self._buckets.setdefault(user_id, [])
        # Prune old entries
        self._buckets[user_id] = bucket = [t for t in bucket if now - t < self._period]
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


_rate_limiter = _RateLimiter()

# Bound concurrent agent executions to prevent thread/API exhaustion under load.
_MAX_CONCURRENT_AGENTS = int(os.getenv("BIOMNI_MAX_CONCURRENT_AGENTS", "10"))
_agent_semaphore: asyncio.Semaphore | None = None


def _get_agent_semaphore() -> asyncio.Semaphore:
    """Lazy-init semaphore (must be created inside a running event loop)."""
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AGENTS)
    return _agent_semaphore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "static"
ENV_FILE = PROJECT_ROOT / ".env"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TAG_RE = {
    "execute": re.compile(r"<execute>(.*?)</execute>", re.DOTALL | re.IGNORECASE),
    "observation": re.compile(r"<observation>(.*?)</observation>", re.DOTALL | re.IGNORECASE),
    "solution": re.compile(r"<solution>(.*?)</solution>", re.DOTALL | re.IGNORECASE),
}

agent: Any | None = None
agent_init_error: str | None = None
active_connections: dict[str, WebSocket] = {}


class WebSocketClosedError(RuntimeError):
    """Raised when the client disconnects before the server can send a frame."""

PUBLIC_CAPABILITY_BUCKETS = [
    {
        "id": "literature-evidence",
        "title": "Literature & Evidence",
        "description": "Search papers, prior work, and public biomedical resources with grounded outputs.",
        "sample_prompt": "Search PubMed for CRISPR-Cas9 delivery methods in vivo and summarize the strongest approaches.",
        "modules": {"literature", "database"},
    },
    {
        "id": "clinical-strategy",
        "title": "Clinical & Translational",
        "description": "Scan trial landscapes and translational context around therapeutic areas.",
        "sample_prompt": "Find active clinical trials for CAR-T therapy in B-ALL and compare their inclusion criteria.",
        "modules": {"database", "pathology", "cancer_biology", "physiology"},
    },
    {
        "id": "drug-discovery",
        "title": "Drug & ADMET",
        "description": "Profile compounds, mechanisms, pharmacology, and developability questions.",
        "sample_prompt": "What are the ADMET properties of remdesivir and what liabilities should a team watch first?",
        "modules": {"pharmacology", "biochemistry", "biophysics"},
    },
    {
        "id": "genomics-design",
        "title": "Genomics & CRISPR",
        "description": "Explore genes, perturbation strategies, and experimental design in genomic workflows.",
        "sample_prompt": "Design a CRISPR screen to identify regulators of T cell exhaustion and propose the first readouts.",
        "modules": {"genomics", "genetics", "synthetic_biology", "systems_biology"},
    },
    {
        "id": "mechanism-biology",
        "title": "Mechanism Biology",
        "description": "Interrogate pathways, immune biology, and molecular mechanisms across disease questions.",
        "sample_prompt": "Explain the mechanism of immune checkpoint inhibitors and propose the most informative biomarkers.",
        "modules": {"immunology", "molecular_biology", "cell_biology", "microbiology"},
    },
    {
        "id": "lab-ops",
        "title": "Protocols & Lab Ops",
        "description": "Use Biomni as a planning partner for protocol, automation, and lab execution tasks.",
        "sample_prompt": "Plan a validation workflow for a lentiviral transduction protocol with the key checkpoints.",
        "modules": {"protocols", "lab_automation", "bioengineering", "bioimaging"},
    },
]

PUBLIC_WORKFLOWS = [
    PublicWorkflowCard(
        id="literature-review",
        title="Literature review sprint",
        description="Start from papers, prior art, and a structured evidence scan instead of a blank chat box.",
        prompt="Search PubMed for emerging non-viral CRISPR delivery approaches in vivo and summarize the strongest evidence, limitations, and open questions.",
        outcome="A grounded starting brief you can turn into experiments or strategy.",
        tags=["PubMed", "evidence", "hypothesis"],
    ),
    PublicWorkflowCard(
        id="clinical-trials",
        title="Clinical trial landscape",
        description="Map active trials, inclusion criteria, and translational context around a therapeutic area.",
        prompt="Find active clinical trials for CAR-T therapy in B-ALL, compare inclusion criteria, and note what differentiates the trial designs.",
        outcome="A fast clinical landscape snapshot you can refine with follow-up questions.",
        tags=["trials", "translational", "comparison"],
    ),
    PublicWorkflowCard(
        id="drug-profile",
        title="Drug mechanism and ADMET",
        description="Profile a compound from mechanism through pharmacology and developability concerns.",
        prompt="What is the mechanism of action of remdesivir, what are its ADMET properties, and where are the biggest translational caveats?",
        outcome="An actionable compound profile with mechanistic and pharmacology framing.",
        tags=["drug", "ADMET", "mechanism"],
    ),
    PublicWorkflowCard(
        id="target-mechanism",
        title="Target and pathway exploration",
        description="Use Biomni as a first-pass mechanism partner for genes, pathways, and immune biology.",
        prompt="Explain the mechanism of immune checkpoint inhibitors and propose the most informative biomarkers for patient stratification.",
        outcome="A concise mechanistic brief you can deepen into experiments or reviews.",
        tags=["pathway", "immunology", "biomarkers"],
    ),
]

PUBLIC_WORKSPACE_HIGHLIGHTS = [
    "Guided scientific workflows instead of a blank prompt wall.",
    "Persistent conversations, sharing, and export for collaborative iteration.",
    "Public capability and readiness metadata available before sign-in.",
    "Live workspace supports follow-up discussion, slash-command research, and voice-to-text input after sign-in.",
]

PUBLIC_TRUST_POINTS = [
    "Public metadata is descriptive only; execution surfaces stay authenticated.",
    "Biomni can stream execute, observation, and solution traces during analysis.",
    "Readiness and capability signals are visible before login so the shell can tell the truth clearly.",
]

PUBLIC_ENDPOINTS = [
    "/api/health",
    "/api/public/overview",
    "/api/tools",
]

AUTH_REQUIRED_SURFACES = [
    "POST /api/chat/query",
    "POST /api/audio/transcribe",
    "POST /api/audio/transcribe/stream",
    "GET /api/conversations",
    "GET /api/conversations/{conversation_id}",
    "DELETE /api/conversations/{conversation_id}",
    "POST /api/share/{conversation_id}",
    "WebSocket /ws/stream",
]

CHAT_CONTEXT_TURN_LIMIT = max(0, int(os.getenv("BIOMNI_CHAT_CONTEXT_TURNS", "8")))
CHAT_CONTEXT_CHAR_LIMIT = max(0, int(os.getenv("BIOMNI_CHAT_CONTEXT_CHARS", "6000")))
RESEARCH_COMMAND = "/research"
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_DEFAULT_LANGUAGE", "").strip()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "").strip()


def _extract_tag_blocks(text: str, tag: str) -> list[str]:
    pattern = TAG_RE[tag]
    return [match.strip() for match in pattern.findall(text) if match.strip()]


def _extract_tools_from_execute(execute_blocks: list[str]) -> list[dict[str, str]]:
    if not execute_blocks:
        return []
    if agent is None:
        return []
    try:
        from biomni.utils import parse_tool_calls_with_modules
    except Exception:
        return []
    module2api = getattr(agent, "module2api", {})
    custom_functions = getattr(agent, "_custom_functions", {})
    detected: set[tuple[str, str]] = set()
    for code in execute_blocks:
        try:
            tool_pairs = parse_tool_calls_with_modules(code, module2api, custom_functions)
        except Exception:
            continue
        for tool_name, module_name in tool_pairs:
            detected.add((tool_name, module_name))
    return [{"name": tool_name, "module": module_name} for tool_name, module_name in sorted(detected)]


def _parse_stream_output(output: str) -> dict[str, Any]:
    clean_output = _strip_ansi(output)
    execute_blocks = _extract_tag_blocks(clean_output, "execute")
    observation_blocks = _extract_tag_blocks(clean_output, "observation")
    solution_blocks = _extract_tag_blocks(clean_output, "solution")
    tool_calls = _extract_tools_from_execute(execute_blocks)
    return {
        "output": clean_output,
        "execute": execute_blocks,
        "observation": observation_blocks,
        "solution": solution_blocks,
        "tool_calls": tool_calls,
    }


def _extract_final_result(log_entries: list[str], fallback: str | None = None) -> str:
    for entry in reversed(log_entries):
        clean = _strip_ansi(entry)
        solutions = _extract_tag_blocks(clean, "solution")
        if solutions:
            return solutions[-1]

    for entry in reversed(log_entries):
        clean = _strip_ansi(entry)
        clean = re.sub(r"<execute>.*?</execute>", "", clean, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<observation>.*?</observation>", "", clean, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<solution>.*?</solution>", "", clean, flags=re.DOTALL | re.IGNORECASE)
        if clean.strip():
            return clean.strip()

    if fallback:
        clean_fallback = _strip_ansi(fallback)
        fallback_solutions = _extract_tag_blocks(clean_fallback, "solution")
        if fallback_solutions:
            return fallback_solutions[-1]
        if clean_fallback:
            return clean_fallback

    return "No result generated."


def _message_from_row(row: dict[str, Any]) -> MessageResponse:
    metadata: dict[str, Any] = {}
    raw = row.get("metadata_json")
    if raw:
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError:
            metadata = {"raw": str(raw)}
    return MessageResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        metadata=metadata,
        created_at=row["created_at"],
    )


def _conversation_detail_from_row(row: dict[str, Any]) -> ConversationDetailResponse:
    messages = [_message_from_row(message) for message in row.get("messages", [])]
    return ConversationDetailResponse(
        id=row["id"],
        title=row.get("title") or "New conversation",
        share_token=row.get("share_token") or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        messages=messages,
    )


def _require_agent() -> Any:
    if agent is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Agent not initialized")
    return agent


def _extract_query_text(payload: dict[str, Any]) -> str:
    return (payload.get("text") or payload.get("prompt") or "").strip()


def _extract_payload_history(payload: dict[str, Any]) -> list[ConversationHistoryTurn]:
    raw_items = payload.get("conversation_history")
    if not isinstance(raw_items, list):
        return []

    items: list[ConversationHistoryTurn] = []
    for raw in raw_items:
        if isinstance(raw, ConversationHistoryTurn):
            items.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        try:
            items.append(ConversationHistoryTurn.model_validate(raw))
        except Exception:
            continue
    return items


def _history_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized == "agent":
        return "assistant"
    if normalized in {"assistant", "user"}:
        return normalized
    return "assistant"


def _normalize_history_entries(history: list[ConversationHistoryTurn] | None) -> list[dict[str, str]]:
    if not history:
        return []

    normalized: list[dict[str, str]] = []
    for item in history:
        text = item.message_text()
        if not text:
            continue
        normalized.append({"role": _history_role(item.role), "text": text})

    if CHAT_CONTEXT_TURN_LIMIT > 0 and len(normalized) > CHAT_CONTEXT_TURN_LIMIT:
        normalized = normalized[-CHAT_CONTEXT_TURN_LIMIT:]

    if CHAT_CONTEXT_CHAR_LIMIT <= 0:
        return normalized

    trimmed: list[dict[str, str]] = []
    remaining = CHAT_CONTEXT_CHAR_LIMIT
    for item in reversed(normalized):
        text = item["text"]
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[-remaining:]
        trimmed.append({"role": item["role"], "text": text})
        remaining -= len(text)
    return list(reversed(trimmed))


def _history_markdown(history: list[dict[str, str]]) -> str:
    if not history:
        return "_No prior discussion._"
    lines = []
    for item in history:
        speaker = "User" if item["role"] == "user" else "Assistant"
        lines.append(f"### {speaker}\n{item['text']}")
    return "\n\n".join(lines)


def _parse_chat_command(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    if not stripped:
        return None, ""
    if stripped == RESEARCH_COMMAND:
        return "research", ""
    prefix = f"{RESEARCH_COMMAND} "
    if stripped.lower().startswith(prefix):
        return "research", stripped[len(prefix):].strip()
    return None, stripped


def _build_effective_prompt(raw_text: str, history: list[dict[str, str]]) -> tuple[str, str]:
    command_mode, command_body = _parse_chat_command(raw_text)
    prompt_body = command_body if command_mode else raw_text.strip()

    if command_mode == "research":
        if not prompt_body and not history:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Research mode needs a question or earlier discussion context.",
            )
        prompt = (
            "You are Biomni in RESEARCH COMMAND mode.\n"
            "Use the earlier discussion as context when it is relevant, but upgrade the next answer into a grounded research pass.\n"
            "Prefer literature, trials, mechanisms, biomarkers, and concrete scientific next steps over generic conversation.\n"
            "Keep the pass bounded and scientist-friendly: do enough retrieval to answer well, but avoid turning every request into a sprawling multi-stage review.\n"
            "Default to a focused brief with at most 3 targeted retrieval or tool steps unless the question explicitly requires deeper coverage or conflicting evidence forces an extra check.\n"
            "Once you have enough evidence to answer, stop researching and synthesize.\n"
            "Return the answer in Markdown with these sections when they fit:\n"
            "## Framing\n## Evidence\n## Key mechanisms or studies\n## Open uncertainties\n## Suggested next steps\n"
            "When possible, cite PMIDs, DOIs, NCT IDs, or arXiv IDs inline.\n"
            "Prefer concise bullets or short paragraphs over long chain-of-thought style planning.\n\n"
            "## Prior discussion\n"
            f"{_history_markdown(history)}\n\n"
            "## Current research request\n"
            f"{prompt_body or 'Use the prior discussion to produce the next grounded research brief.'}"
        )
        return prompt, "research"

    if history:
        prompt = (
            "Continue the ongoing Biomni conversation.\n"
            "Use the prior discussion below as relevant context, but answer the user's latest message directly.\n"
            "Do not restate the full history unless it helps the scientist move forward.\n\n"
            "## Prior discussion\n"
            f"{_history_markdown(history)}\n\n"
            "## Current user message\n"
            f"{prompt_body}"
        )
        return prompt, "chat"

    return prompt_body, "chat"


def _cors_origins() -> list[str]:
    raw_origins = os.getenv(
        "BIOMNI_CORS_ORIGINS",
        "http://localhost:8129,http://127.0.0.1:8129",
    )
    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["http://localhost:8129"]


def _collect_tool_catalog(biomni_agent: Any | None) -> tuple[int, dict[str, list[ToolInfo]]]:
    if biomni_agent is None:
        return 0, {}

    categories: dict[str, list[ToolInfo]] = {}
    tools_count = 0
    for module_name, module_tools in getattr(biomni_agent, "module2api", {}).items():
        category = module_name.split(".")[-1]
        category_tools: list[ToolInfo] = []
        for tool in module_tools:
            info = ToolInfo(
                name=tool.get("name", "unknown"),
                description=tool.get("description", "No description available"),
                module=module_name,
                required_parameters=tool.get("required_parameters", []),
                optional_parameters=tool.get("optional_parameters", []),
            )
            category_tools.append(info)
            tools_count += 1
        categories[category] = category_tools

    custom_tools = getattr(biomni_agent, "_custom_tools", {})
    if custom_tools:
        categories.setdefault("custom", [])
        for name, tool in custom_tools.items():
            categories["custom"].append(
                ToolInfo(
                    name=name,
                    description=tool.get("description", "Custom tool"),
                    module=tool.get("module", "custom"),
                    required_parameters=[],
                    optional_parameters=[],
                )
            )
            tools_count += 1

    return tools_count, categories


def _build_public_capabilities(biomni_agent: Any | None) -> list[PublicCapabilitySummary]:
    bucket_counts = {bucket["id"]: 0 for bucket in PUBLIC_CAPABILITY_BUCKETS}
    uncategorized_tools = 0

    if biomni_agent is not None:
        for module_name, module_tools in getattr(biomni_agent, "module2api", {}).items():
            module_key = module_name.split(".")[-1]
            for bucket in PUBLIC_CAPABILITY_BUCKETS:
                if module_key in bucket["modules"]:
                    bucket_counts[bucket["id"]] += len(module_tools)
                    break
            else:
                uncategorized_tools += len(module_tools)

    capabilities = [
        PublicCapabilitySummary(
            id=bucket["id"],
            title=bucket["title"],
            description=bucket["description"],
            tool_count=bucket_counts[bucket["id"]],
            sample_prompt=bucket["sample_prompt"],
        )
        for bucket in PUBLIC_CAPABILITY_BUCKETS
    ]

    if uncategorized_tools > 0:
        capabilities.append(
            PublicCapabilitySummary(
                id="general-biomedical",
                title="General Biomedical Utilities",
                description="Additional domain-specific tools available inside the authenticated workspace.",
                tool_count=uncategorized_tools,
                sample_prompt="Explore the additional Biomni toolset for a specific biomedical question.",
            )
        )

    return capabilities


def _build_public_readiness(tools_count: int) -> list[PublicReadinessCheck]:
    agent_ready = agent is not None
    stt_ready = transcription_available()
    stt_providers = transcription_provider_names()
    return [
        PublicReadinessCheck(
            id="scientific-engine",
            label="Scientific engine",
            status="ready" if agent_ready else "degraded",
            detail=(
                "Biomni is initialized and can execute grounded biomedical workflows."
                if agent_ready
                else f"Biomni is not ready yet: {agent_init_error or 'agent initialization is still unavailable.'}"
            ),
        ),
        PublicReadinessCheck(
            id="capability-catalog",
            label="Capability catalog",
            status="ready" if tools_count > 0 else "degraded",
            detail=(
                f"{tools_count} read-only tool descriptions are available for pre-login discovery."
                if tools_count > 0
                else "Capability metadata is not populated yet because the agent catalog is unavailable."
            ),
        ),
        PublicReadinessCheck(
            id="interactive-workspace",
            label="Interactive workspace",
            status="auth-required",
            detail="Live chat, streaming execution, conversation history, and share-token creation require sign-in.",
        ),
        PublicReadinessCheck(
            id="voice-input",
            label="Voice input",
            status="auth-required" if stt_ready else "degraded",
            detail=(
                f"Voice-to-text is available after sign-in via {', '.join(stt_providers)}."
                if stt_ready
                else "Voice transcription is not configured yet because no STT provider key is available."
            ),
        ),
    ]


def _build_public_overview() -> PublicOverviewResponse:
    tools_count, _ = _collect_tool_catalog(agent)
    agent_ready = agent is not None
    return PublicOverviewResponse(
        status="ok" if agent_ready else "degraded",
        headline="Biomni turns biomedical questions into grounded research workspaces.",
        summary=(
            "Use Biomni to move from question to literature, trials, mechanisms, and protocol-oriented reasoning "
            "with a clearer scientist-facing shell."
        ),
        workspace_highlights=PUBLIC_WORKSPACE_HIGHLIGHTS,
        trust_points=PUBLIC_TRUST_POINTS,
        agent_initialized=agent_init_error is None,
        agent_ready=agent_ready,
        agent_error=agent_init_error,
        tools_count=tools_count,
        capabilities=_build_public_capabilities(agent),
        workflows=PUBLIC_WORKFLOWS,
        public_endpoints=PUBLIC_ENDPOINTS,
        auth_required_surfaces=AUTH_REQUIRED_SURFACES,
        readiness_checks=_build_public_readiness(tools_count),
    )


async def _conversation_history_for_agent(
    *,
    payload_history: list[ConversationHistoryTurn] | None = None,
    conversation_id: str | None = None,
    current_user_id: str | None = None,
) -> list[dict[str, str]]:
    normalized = _normalize_history_entries(payload_history or [])
    if normalized:
        return normalized

    if conversation_id and current_user_id:
        conversation = await get_conversation_with_messages_for_user(conversation_id, current_user_id)
        if conversation:
            history = [
                ConversationHistoryTurn(role=message.get("role", "assistant"), content=message.get("content") or "")
                for message in conversation.get("messages", [])
            ]
            return _normalize_history_entries(history)

    return []


async def _resolve_websocket_user(websocket: WebSocket, payload: dict[str, Any]) -> dict[str, Any] | None:
    token = (
        websocket.query_params.get("token")
        or payload.get("token")
        or payload.get("access_token")
        or payload.get("jwt")
    )
    if not token:
        return None
    try:
        token_payload = decode_access_token(str(token))
    except JWTError:
        return None
    user_id = token_payload.get("sub")
    if not user_id:
        return None
    return await get_user_by_id(str(user_id))


async def _ws_send(websocket: WebSocket, message: StreamMessage) -> None:
    try:
        await websocket.send_text(message.model_dump_json())
    except WebSocketDisconnect as exc:
        raise WebSocketClosedError("WebSocket disconnected before the message could be delivered.") from exc
    except RuntimeError as exc:
        if "close message has been sent" in str(exc).lower():
            raise WebSocketClosedError("WebSocket closed before the message could be delivered.") from exc
        raise


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global agent, agent_init_error

    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=True)

    await init_db()

    llm = os.getenv("BIOMNI_LLM")
    data_path = os.getenv("BIOMNI_PATH", str(PROJECT_ROOT / "data"))
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")

    # JWT secret validation now happens inside auth._resolve_jwt_secret()
    # which logs a warning and generates an ephemeral secret when unconfigured.

    try:
        logger.info("Initializing Biomni A1 agent")
        from biomni.agent.a1 import A1

        agent = A1(
            path=data_path,
            llm=llm,
            api_key=anthropic_api_key,
            expected_data_lake_files=[],
        )
        agent_init_error = None
        logger.info("Biomni A1 agent initialized")
    except Exception as exc:  # pragma: no cover - environment dependent
        agent = None
        agent_init_error = str(exc)
        logger.exception("Failed to initialize Biomni A1")

    try:
        yield
    finally:
        active_connections.clear()


app = FastAPI(
    title="Biomni Portal",
    description="Biomni A1 backend with auth, streaming, and persistence",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
    return response


app.include_router(auth_router)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    agent_ready = agent is not None
    return HealthResponse(
        status="ok" if agent_ready else "degraded",
        agent_initialized=agent_init_error is None,
        agent_ready=agent_ready,
        agent_error=agent_init_error,
    )


@app.get("/api/public/overview", response_model=PublicOverviewResponse)
async def public_overview() -> PublicOverviewResponse:
    return _build_public_overview()


@app.post("/api/chat/query", response_model=ChatQueryResponse)
async def chat_query(
    request: ChatQueryRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatQueryResponse:
    if not _rate_limiter.check(current_user["id"]):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait before sending another query.")
    biomni_agent = _require_agent()
    raw_query_text = request.query_text()
    if not raw_query_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query text is required")

    conversation_id = request.conversation_id or request.session_id
    history_entries = await _conversation_history_for_agent(
        payload_history=request.conversation_history,
        conversation_id=conversation_id,
        current_user_id=current_user["id"],
    )
    effective_prompt, command_mode = _build_effective_prompt(raw_query_text, history_entries)
    if conversation_id:
        conversation = await get_conversation_by_id(conversation_id, user_id=current_user["id"])
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    else:
        conversation = await create_conversation(current_user["id"])
        conversation_id = conversation["id"]

    await add_message(
        conversation_id,
        "user",
        raw_query_text,
        metadata={
            "source": "rest",
            "command_mode": command_mode,
            "history_turns_used": len(history_entries),
        },
    )

    query_id = str(uuid.uuid4())
    start = time.perf_counter()
    try:
        log_entries, final_raw = await asyncio.to_thread(biomni_agent.go, effective_prompt)
    except Exception as exc:
        logger.exception("Agent query failed")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Agent query failed. Please try again.") from exc

    tools_used: set[str] = set()
    trace_steps: list[dict[str, Any]] = []
    for entry in log_entries:
        parsed = _parse_stream_output(entry)
        trace_steps.append(parsed)
        for tool in parsed["tool_calls"]:
            tools_used.add(tool["name"])

    final_result = _extract_final_result(log_entries, str(final_raw))
    execution_time = time.perf_counter() - start

    await add_message(
        conversation_id,
        "agent",
        final_result,
        metadata={
            "source": "rest",
            "query_id": query_id,
            "command_mode": command_mode,
            "history_turns_used": len(history_entries),
            "tools_used": sorted(tools_used),
            "execution_time": execution_time,
            "trace": trace_steps,
        },
    )

    return ChatQueryResponse(
        query_id=query_id,
        conversation_id=conversation_id,
        result=final_result,
        tools_used=sorted(tools_used),
        execution_time=execution_time,
    )


@app.post("/api/audio/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    _current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    audio_data = await file.read()
    if not audio_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audio file is empty.")
    if len(audio_data) > MAX_AUDIO_FILE_BYTES:
        max_mb = MAX_AUDIO_FILE_BYTES // (1024 * 1024)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"Audio file exceeds the {max_mb} MB limit.")

    try:
        result = await transcribe_audio_file(
            audio_data=audio_data,
            filename=file.filename or "audio.webm",
            content_type=file.content_type or "audio/webm",
            language=TRANSCRIBE_LANGUAGE,
        )
        return {"status": "ok", **result}
    except Exception as exc:
        logger.error("Audio transcription failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Transcription failed. Try again.") from exc


@app.post("/api/audio/transcribe/stream")
async def transcribe_audio_stream(
    file: UploadFile = File(...),
    _current_user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    audio_data = await file.read()
    if not audio_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audio file is empty.")
    if len(audio_data) > MAX_AUDIO_FILE_BYTES:
        max_mb = MAX_AUDIO_FILE_BYTES // (1024 * 1024)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"Audio file exceeds the {max_mb} MB limit.")

    stream = stream_transcription_ndjson(
        audio_data=audio_data,
        filename=file.filename or "audio.webm",
        content_type=file.content_type or "audio/webm",
        language=TRANSCRIBE_LANGUAGE,
    )
    return StreamingResponse(stream, media_type="application/x-ndjson")


@app.get("/api/tools", response_model=ToolsResponse)
async def tools() -> ToolsResponse:
    biomni_agent = _require_agent()
    tools_count, categories = _collect_tool_catalog(biomni_agent)
    return ToolsResponse(tools_count=tools_count, categories=categories)


@app.get("/api/conversations", response_model=list[ConversationSummaryResponse])
async def list_conversations(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[ConversationSummaryResponse]:
    conversations = await list_conversations_for_user(current_user["id"])
    return [
        ConversationSummaryResponse(
            id=item["id"],
            title=item.get("title") or "New conversation",
            share_token=item.get("share_token") or "",
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            message_count=int(item.get("message_count") or 0),
        )
        for item in conversations
    ]


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ConversationDetailResponse:
    conversation = await get_conversation_with_messages_for_user(conversation_id, current_user["id"])
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return _conversation_detail_from_row(conversation)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    deleted = await delete_conversation_for_user(conversation_id, current_user["id"])
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return {"deleted": True, "conversation_id": conversation_id}


@app.post("/api/share/{conversation_id}", response_model=ShareTokenResponse)
async def create_share_token(
    conversation_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ShareTokenResponse:
    share_token = await ensure_share_token_for_conversation(conversation_id, current_user["id"])
    if not share_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    base_url = str(request.base_url).rstrip("/")
    return ShareTokenResponse(
        conversation_id=conversation_id,
        share_token=share_token,
        share_url=f"{base_url}/shared/{share_token}",
    )


@app.get("/api/share/{conversation_id}", response_model=ConversationDetailResponse)
async def shared_conversation(conversation_id: str) -> ConversationDetailResponse:
    # Supports both legacy IDs and share tokens for read-only sharing.
    conversation = await get_shared_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shared conversation not found")
    return _conversation_detail_from_row(conversation)


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket) -> None:
    connection_id = str(uuid.uuid4())
    await websocket.accept()
    active_connections[connection_id] = websocket
    current_user: dict[str, Any] | None = None

    try:
        while True:
            try:
                payload = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                await _ws_send(
                    websocket,
                    StreamMessage(
                        type="error",
                        data={"error": "Invalid JSON payload"},
                        query_id="",
                    ),
                )
                continue

            if current_user is None:
                current_user = await _resolve_websocket_user(websocket, payload)
                if current_user is None:
                    await _ws_send(
                        websocket,
                        StreamMessage(
                            type="error",
                            data={"error": "Authentication token is required"},
                            query_id="",
                        ),
                    )
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return

            raw_query_text = _extract_query_text(payload)
            if not raw_query_text:
                await _ws_send(
                    websocket,
                    StreamMessage(
                        type="error",
                        data={"error": "Query text is required"},
                        query_id="",
                    ),
                )
                continue

            if not _rate_limiter.check(current_user["id"]):
                await _ws_send(
                    websocket,
                    StreamMessage(
                        type="error",
                        data={"error": "Rate limit exceeded. Please wait before sending another query."},
                        query_id="",
                    ),
                )
                continue

            if agent is None:
                await _ws_send(
                    websocket,
                    StreamMessage(
                        type="error",
                        data={"error": "Agent not initialized"},
                        query_id="",
                    ),
                )
                await _ws_send(
                    websocket,
                    StreamMessage(type="end", data={"reason": "agent_unavailable"}, query_id=""),
                )
                continue

            conversation_id = payload.get("conversation_id") or payload.get("session_id")
            if conversation_id:
                conversation = await get_conversation_by_id(str(conversation_id), user_id=current_user["id"])
                if not conversation:
                    await _ws_send(
                        websocket,
                        StreamMessage(
                            type="error",
                            data={"error": "Conversation not found"},
                            query_id="",
                        ),
                    )
                    continue
            else:
                conversation = await create_conversation(current_user["id"])
                conversation_id = conversation["id"]

            history_entries = await _conversation_history_for_agent(
                payload_history=_extract_payload_history(payload),
                conversation_id=str(conversation_id),
                current_user_id=current_user["id"],
            )
            effective_prompt, command_mode = _build_effective_prompt(raw_query_text, history_entries)

            await add_message(
                str(conversation_id),
                "user",
                raw_query_text,
                metadata={
                    "source": "websocket",
                    "command_mode": command_mode,
                    "history_turns_used": len(history_entries),
                },
            )

            query_id = str(uuid.uuid4())
            await _ws_send(
                websocket,
                StreamMessage(
                    type="start",
                    data={
                        "query": raw_query_text,
                        "command_mode": command_mode,
                        "conversation_id": conversation_id,
                        "user_id": current_user["id"],
                    },
                    query_id=query_id,
                ),
            )

            step_count = 0
            tools_used: list[str] = []
            start = time.perf_counter()

            try:
                sem = _get_agent_semaphore()
                if sem.locked() and sem._value == 0:  # type: ignore[attr-defined]
                    await _ws_send(
                        websocket,
                        StreamMessage(
                            type="step",
                            data={"output": "Server is busy — your request is queued, please wait..."},
                            query_id=query_id,
                        ),
                    )
                await sem.acquire()
                try:
                    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

                    def _run_stream(
                        active_prompt: str = effective_prompt,
                        stream_queue: asyncio.Queue[dict[str, Any] | None] = queue,
                    ) -> None:
                        try:
                            for step in agent.go_stream(active_prompt):
                                stream_queue.put_nowait(step)
                            stream_queue.put_nowait(None)  # sentinel
                        except Exception as exc:
                            stream_queue.put_nowait({"__error__": str(exc)})

                    stream_task = asyncio.get_event_loop().run_in_executor(None, _run_stream)

                    while True:
                        step = await queue.get()
                        if step is None:
                            break
                        if "__error__" in step:
                            raise RuntimeError(step["__error__"])

                        step_count += 1
                        parsed = _parse_stream_output(step.get("output", ""))
                        step_tool_names = [tool["name"] for tool in parsed["tool_calls"]]
                        for tool_name in step_tool_names:
                            if tool_name not in tools_used:
                                tools_used.append(tool_name)

                        await _ws_send(
                            websocket,
                            StreamMessage(
                                type="step",
                                data={
                                    "step_number": step_count,
                                    "output": parsed["output"],
                                    "execute": parsed["execute"],
                                    "observation": parsed["observation"],
                                    "solution": parsed["solution"],
                                    "tool_calls": parsed["tool_calls"],
                                    "tools_used_so_far": tools_used,
                                    "conversation_id": conversation_id,
                                },
                                query_id=query_id,
                            ),
                        )

                    await stream_task
                finally:
                    sem.release()
            except WebSocketClosedError:
                raise
            except Exception as exc:
                logger.exception("Agent execution failed for query %s", query_id)
                await _ws_send(
                    websocket,
                    StreamMessage(type="error", data={"error": "Agent execution failed. Please try again."}, query_id=query_id),
                )
                await _ws_send(
                    websocket,
                    StreamMessage(
                        type="end",
                        data={"conversation_id": conversation_id, "failed": True},
                        query_id=query_id,
                    ),
                )
                continue

            execution_time = time.perf_counter() - start
            log_entries = getattr(agent, "log", [])
            trace_steps: list[dict[str, Any]] = []
            for entry in log_entries:
                trace_steps.append(_parse_stream_output(entry))
            final_result = _extract_final_result(log_entries)
            await add_message(
                str(conversation_id),
                "agent",
                final_result,
                metadata={
                    "source": "websocket",
                    "query_id": query_id,
                    "command_mode": command_mode,
                    "history_turns_used": len(history_entries),
                    "step_count": step_count,
                    "tools_used": tools_used,
                    "execution_time": execution_time,
                    "trace": trace_steps,
                },
            )

            await _ws_send(
                websocket,
                StreamMessage(
                    type="result",
                    data={
                        "result": final_result,
                        "tools_used": tools_used,
                        "total_steps": step_count,
                        "execution_time": execution_time,
                        "conversation_id": conversation_id,
                    },
                    query_id=query_id,
                ),
            )

            await _ws_send(
                websocket,
                StreamMessage(
                    type="end",
                    data={"conversation_id": conversation_id, "total_steps": step_count},
                    query_id=query_id,
                ),
            )

    except (WebSocketDisconnect, WebSocketClosedError):
        logger.info("WebSocket disconnected: %s", connection_id)
    except Exception:
        logger.exception("WebSocket stream failed: %s", connection_id)
    finally:
        active_connections.pop(connection_id, None)


@app.websocket("/{rest_of_path:path}")
async def websocket_not_found(websocket: WebSocket, rest_of_path: str) -> None:
    if rest_of_path == "ws/stream":
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)


@app.exception_handler(Exception)
async def global_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled backend error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return FileResponse("static/robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    return FileResponse("static/sitemap.xml", media_type="application/xml")


_NOT_FOUND_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="3;url=/">
  <title>404 — Biomni Portal</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{min-height:100vh;display:flex;align-items:center;justify-content:center;
         font-family:system-ui,-apple-system,sans-serif;background:#0a0a0f;color:#e0e0e0}
    .card{text-align:center;max-width:480px;padding:3rem 2rem}
    .brand{font-size:1.6rem;font-weight:700;color:#60a5fa;margin-bottom:.5rem}
    .code{font-size:4rem;font-weight:800;color:#f87171;line-height:1}
    .msg{margin:1rem 0;color:#a0a0b0;font-size:1.05rem}
    a{color:#60a5fa;text-decoration:none}
    a:hover{text-decoration:underline}
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">Biomni Portal</div>
    <div class="code">404</div>
    <p class="msg">This page does not exist. Redirecting to <a href="/">home</a> in 3 seconds&hellip;</p>
  </div>
</body>
</html>
"""


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.exception_handler(404)
async def not_found_handler(request: Request, _exc: Exception) -> HTMLResponse | JSONResponse:
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)
    return JSONResponse(status_code=404, content={"detail": "Not found"})
