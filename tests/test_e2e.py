"""
End-to-end tests for Biomni Portal.

Runs against a LIVE server (localhost:8129) with real API keys.
The agent must be initialized and ready.

Usage:
    source .venv/bin/activate
    python -m pytest tests/test_e2e.py -v -s
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

try:
    import websockets  # type: ignore[import-not-found]
except ImportError:
    websockets = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = os.getenv("SMOKE_HOST", "127.0.0.1")
PORT = int(os.getenv("SMOKE_PORT", "8129"))
BASE = f"http://{HOST}:{PORT}"
WS_BASE = f"ws://{HOST}:{PORT}"

TIMEOUT_AGENT = 120  # agent queries can be slow (tool calls, LLM reasoning)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ensure_server():
    """Fail fast if server is not running."""
    with httpx.Client(timeout=5.0) as client:
        try:
            r = client.get(f"{BASE}/api/health")
            assert r.status_code == 200, f"Health check failed: {r.status_code}"
            data = r.json()
            assert data.get("agent_ready"), f"Agent not ready: {data.get('agent_error')}"
        except httpx.ConnectError:
            pytest.skip("Server not running at localhost:8129")


@pytest.fixture(scope="session")
def test_user(ensure_server):
    """Register a unique test user and return (email, password, token, user_id)."""
    uid = int(time.time() * 1000) % 10_000_000
    email = f"e2e_{uid}@test.biomni.dev"
    password = "E2eTestPass!42"
    display_name = f"E2E Tester {uid}"

    with httpx.Client(timeout=10.0) as client:
        r = client.post(f"{BASE}/api/auth/register", json={
            "email": email,
            "display_name": display_name,
            "password": password,
        })
        assert r.status_code == 201, f"Register failed: {r.status_code} {r.text}"
        body = r.json()
        token = body["access_token"]
        user_id = body["user"]["id"]

    return {"email": email, "password": password, "token": token, "user_id": user_id}


@pytest.fixture(scope="session")
def auth_headers(test_user):
    return {"Authorization": f"Bearer {test_user['token']}"}


@pytest.fixture(scope="session")
def client(ensure_server):
    with httpx.Client(timeout=TIMEOUT_AGENT, base_url=BASE) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestAuth:
    def test_login(self, client, test_user):
        r = client.post("/api/auth/login", json={
            "email": test_user["email"],
            "password": test_user["password"],
        })
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["user"]["email"] == test_user["email"]

    def test_login_wrong_password(self, client, test_user):
        r = client.post("/api/auth/login", json={
            "email": test_user["email"],
            "password": "WrongPassword123",
        })
        assert r.status_code == 401

    def test_me(self, client, auth_headers):
        r = client.get("/api/auth/me", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert "email" in body

    def test_me_no_token(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code in (401, 403)

    def test_duplicate_register(self, client, test_user):
        r = client.post("/api/auth/register", json={
            "email": test_user["email"],
            "display_name": "Duplicate",
            "password": "AnotherPass123",
        })
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Tools endpoint
# ---------------------------------------------------------------------------

class TestTools:
    def test_tools_returns_categories(self, client, auth_headers):
        r = client.get("/api/tools", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["tools_count"] > 0, "Agent should have tools loaded"
        assert "categories" in body
        assert len(body["categories"]) > 0

    def test_tools_requires_auth(self, client):
        r = client.get("/api/tools")
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# REST chat query (real agent call)
# ---------------------------------------------------------------------------

class TestChatQuery:
    def test_simple_query(self, client, auth_headers):
        """Send a simple biomedical query via REST and verify the agent responds."""
        r = client.post("/api/chat/query", headers=auth_headers, json={
            "text": "What is the mechanism of action of ibuprofen? Answer in 2 sentences.",
        }, timeout=TIMEOUT_AGENT)
        assert r.status_code == 200, f"Query failed: {r.status_code} {r.text}"
        body = r.json()

        assert body.get("result"), "Agent returned empty result"
        assert body.get("conversation_id"), "No conversation_id returned"
        assert body.get("query_id"), "No query_id returned"
        assert isinstance(body.get("execution_time"), (int, float))
        assert body["execution_time"] > 0

        # Store for conversation tests
        TestChatQuery._conversation_id = body["conversation_id"]
        TestChatQuery._result = body["result"]

    def test_query_requires_auth(self, client):
        r = client.post("/api/chat/query", json={"text": "test"})
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------

class TestConversations:
    def test_list_conversations(self, client, auth_headers):
        r = client.get("/api/conversations", headers=auth_headers)
        assert r.status_code == 200
        conversations = r.json()
        assert isinstance(conversations, list)
        assert len(conversations) > 0, "Should have at least one conversation after query"

    def test_get_conversation_detail(self, client, auth_headers):
        conv_id = getattr(TestChatQuery, "_conversation_id", None)
        if not conv_id:
            pytest.skip("No conversation from previous test")

        r = client.get(f"/api/conversations/{conv_id}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == conv_id
        assert len(body["messages"]) >= 2, "Should have user + agent messages"
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][1]["role"] == "agent"
        assert "ibuprofen" in body["messages"][0]["content"].lower()

    def test_share_conversation(self, client, auth_headers):
        conv_id = getattr(TestChatQuery, "_conversation_id", None)
        if not conv_id:
            pytest.skip("No conversation from previous test")

        r = client.post(f"/api/share/{conv_id}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["share_token"]
        assert body["share_url"]

        # Verify shared view works without auth
        r2 = client.get(f"/api/share/{body['share_token']}")
        assert r2.status_code == 200
        shared = r2.json()
        assert len(shared["messages"]) >= 2

    def test_get_nonexistent_conversation(self, client, auth_headers):
        r = client.get("/api/conversations/nonexistent-id-12345", headers=auth_headers)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket streaming (real agent call)
# ---------------------------------------------------------------------------

class TestWebSocket:
    @pytest.mark.skipif(websockets is None, reason="websockets not installed")
    def test_ws_stream_real_query(self, test_user):
        """Send a real biomedical query over WebSocket and verify streaming steps."""
        result = asyncio.run(self._run_ws_query(test_user["token"]))
        assert result["connected"], "WebSocket connection failed"
        assert result["authenticated"], "WebSocket authentication failed"
        assert result["got_start"], "Never received 'start' message"
        assert result["got_end"], "Never received 'end' message"
        assert result["conversation_id"], "No conversation_id from stream"
        # Agent might respond with result or steps depending on query complexity
        assert result["got_result"] or result["step_count"] > 0, \
            "No result or steps received from agent"

    @staticmethod
    async def _run_ws_query(token: str) -> dict:
        result = {
            "connected": False,
            "authenticated": False,
            "got_start": False,
            "got_result": False,
            "got_end": False,
            "step_count": 0,
            "conversation_id": "",
            "errors": [],
        }

        url = f"{WS_BASE}/ws/stream?token={token}"

        try:
            async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
                result["connected"] = True

                await ws.send(json.dumps({
                    "text": "What is aspirin? One sentence only.",
                }))

                deadline = time.time() + TIMEOUT_AGENT
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        break

                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")
                    data = msg.get("data", {})

                    if msg_type == "start":
                        result["got_start"] = True
                        result["authenticated"] = True
                        result["conversation_id"] = data.get("conversation_id", "")

                    elif msg_type == "step":
                        result["step_count"] += 1

                    elif msg_type == "result":
                        result["got_result"] = True

                    elif msg_type == "end":
                        result["got_end"] = True
                        break

                    elif msg_type == "error":
                        result["errors"].append(data.get("error", str(data)))
                        # Auth errors close the connection
                        if "auth" in str(data).lower() or "token" in str(data).lower():
                            break

        except Exception as exc:
            result["errors"].append(str(exc))

        return result


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["agent_ready"] is True
        assert body["agent_initialized"] is True

    def test_static_files(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Biomni" in r.text


# ---------------------------------------------------------------------------
# Cleanup: delete test conversation
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_delete_conversation(self, client, auth_headers):
        conv_id = getattr(TestChatQuery, "_conversation_id", None)
        if not conv_id:
            pytest.skip("No conversation to delete")

        r = client.delete(f"/api/conversations/{conv_id}", headers=auth_headers)
        assert r.status_code == 200

        # Verify it's gone
        r2 = client.get(f"/api/conversations/{conv_id}", headers=auth_headers)
        assert r2.status_code == 404
