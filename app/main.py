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
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    ConversationSummaryResponse,
    HealthResponse,
    MessageResponse,
    ShareTokenResponse,
    StreamMessage,
    ToolInfo,
    ToolsResponse,
)

logger = logging.getLogger(__name__)

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


def _cors_origins() -> list[str]:
    raw_origins = os.getenv(
        "BIOMNI_CORS_ORIGINS",
        "http://localhost:8129,http://127.0.0.1:8129",
    )
    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["http://localhost:8129"]


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
    await websocket.send_text(message.model_dump_json())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global agent, agent_init_error

    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=True)

    await init_db()

    llm = os.getenv("BIOMNI_LLM")
    data_path = os.getenv("BIOMNI_PATH", str(PROJECT_ROOT / "data"))
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")

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


@app.post("/api/chat/query", response_model=ChatQueryResponse)
async def chat_query(
    request: ChatQueryRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatQueryResponse:
    biomni_agent = _require_agent()
    query_text = request.query_text()
    if not query_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query text is required")

    conversation_id = request.conversation_id or request.session_id
    if conversation_id:
        conversation = await get_conversation_by_id(conversation_id, user_id=current_user["id"])
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    else:
        conversation = await create_conversation(current_user["id"])
        conversation_id = conversation["id"]

    await add_message(conversation_id, "user", query_text, metadata={"source": "rest"})

    query_id = str(uuid.uuid4())
    start = time.perf_counter()
    try:
        log_entries, final_raw = await asyncio.to_thread(biomni_agent.go, query_text)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Query failed: {exc}") from exc

    tools_used: set[str] = set()
    for entry in log_entries:
        parsed = _parse_stream_output(entry)
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
            "tools_used": sorted(tools_used),
            "execution_time": execution_time,
        },
    )

    return ChatQueryResponse(
        query_id=query_id,
        conversation_id=conversation_id,
        result=final_result,
        tools_used=sorted(tools_used),
        execution_time=execution_time,
    )


@app.get("/api/tools", response_model=ToolsResponse)
async def tools(_current_user: dict[str, Any] = Depends(get_current_user)) -> ToolsResponse:
    biomni_agent = _require_agent()

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

            query_text = _extract_query_text(payload)
            if not query_text:
                await _ws_send(
                    websocket,
                    StreamMessage(
                        type="error",
                        data={"error": "Query text is required"},
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

            await add_message(str(conversation_id), "user", query_text, metadata={"source": "websocket"})

            query_id = str(uuid.uuid4())
            await _ws_send(
                websocket,
                StreamMessage(
                    type="start",
                    data={
                        "query": query_text,
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
                for step in agent.go_stream(query_text):
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
                    await asyncio.sleep(0)
            except Exception as exc:
                await _ws_send(
                    websocket,
                    StreamMessage(type="error", data={"error": str(exc)}, query_id=query_id),
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
            final_result = _extract_final_result(getattr(agent, "log", []))
            await add_message(
                str(conversation_id),
                "agent",
                final_result,
                metadata={
                    "source": "websocket",
                    "query_id": query_id,
                    "step_count": step_count,
                    "tools_used": tools_used,
                    "execution_time": execution_time,
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

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", connection_id)
    except Exception:
        logger.exception("WebSocket stream failed: %s", connection_id)
    finally:
        active_connections.pop(connection_id, None)


@app.exception_handler(Exception)
async def global_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled backend error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
