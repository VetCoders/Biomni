from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: Literal["admin", "user"] = "user"
    created_at: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserResponse


class AuthMessageResponse(BaseModel):
    message: str


class ChatQueryRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    text: str | None = Field(default=None, max_length=10000)
    prompt: str | None = Field(default=None, max_length=10000)
    conversation_id: str | None = None
    session_id: str | None = None

    def query_text(self) -> str:
        return (self.text or self.prompt or "").strip()


class ChatQueryResponse(BaseModel):
    query_id: str
    conversation_id: str
    result: str
    tools_used: list[str] = Field(default_factory=list)
    execution_time: float
    timestamp: str = Field(default_factory=now_iso)


class ToolInfo(BaseModel):
    name: str
    description: str
    module: str
    required_parameters: list[dict[str, Any]] = Field(default_factory=list)
    optional_parameters: list[dict[str, Any]] = Field(default_factory=list)


class ToolsResponse(BaseModel):
    tools_count: int
    categories: dict[str, list[ToolInfo]]


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "agent", "system"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ConversationSummaryResponse(BaseModel):
    id: str
    title: str
    share_token: str
    created_at: str
    updated_at: str
    message_count: int = 0


class ConversationDetailResponse(BaseModel):
    id: str
    title: str
    share_token: str
    created_at: str
    updated_at: str
    messages: list[MessageResponse] = Field(default_factory=list)


class ShareTokenResponse(BaseModel):
    conversation_id: str
    share_token: str
    share_url: str


class StreamMessage(BaseModel):
    type: Literal["start", "step", "result", "error", "end"]
    data: dict[str, Any]
    timestamp: str = Field(default_factory=now_iso)
    query_id: str


class HealthResponse(BaseModel):
    status: str
    service: str = "biomni-portal"
    timestamp: str = Field(default_factory=now_iso)
    agent_initialized: bool
    agent_ready: bool
    agent_error: str | None = None
