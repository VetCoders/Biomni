#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    import websockets
except ImportError:  # pragma: no cover - handled at runtime for smoke checks
    websockets = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = os.getenv("SMOKE_HOST", "127.0.0.1")
PORT = int(os.getenv("SMOKE_PORT", "8129"))
BASE_HTTP_URL = f"http://{HOST}:{PORT}"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def wait_for_server(timeout_seconds: int = 60) -> tuple[bool, str]:
    deadline = time.time() + timeout_seconds
    last_error = "unknown error"
    with httpx.Client(timeout=3.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(f"{BASE_HTTP_URL}/api/health")
                if response.status_code == 200:
                    return True, "server is ready"
                last_error = f"health returned {response.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(1)
    return False, last_error


def try_get_openapi(client: httpx.Client) -> dict[str, Any]:
    try:
        response = client.get(f"{BASE_HTTP_URL}/openapi.json")
        if response.status_code == 200:
            return response.json()
    except Exception:  # noqa: BLE001
        pass
    return {}


def find_paths(openapi_spec: dict[str, Any], needle: str, method: str = "post") -> list[str]:
    paths = openapi_spec.get("paths", {})
    out: list[str] = []
    for path, methods in paths.items():
        if needle in path.lower() and method.lower() in {m.lower() for m in methods.keys()}:
            out.append(path)
    return out


def parse_ws_paths_from_static() -> list[str]:
    ws_paths: list[str] = []
    regex = re.compile(r"""['\"]((?:/api)?/ws[A-Za-z0-9_/\-?=&]*)['\"]""")
    for file_path in [
        PROJECT_ROOT / "static" / "app.js",
        PROJECT_ROOT / "static" / "js" / "app.js",
    ]:
        if not file_path.exists():
            continue
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        ws_paths.extend(regex.findall(text))
    return ws_paths


def check_auth(client: httpx.Client, openapi_spec: dict[str, Any]) -> tuple[CheckResult, str | None]:
    register_paths = find_paths(openapi_spec, "register", method="post") or ["/api/auth/register", "/api/register"]
    login_paths = find_paths(openapi_spec, "login", method="post") or ["/api/auth/login", "/api/login", "/token"]

    test_id = int(time.time())
    email = f"smoke{test_id}@example.com"
    username = f"smoke_{test_id}"
    display_name = f"Smoke User {test_id}"
    password = "SmokePass123!"

    register_payloads = [
        {"email": email, "display_name": display_name, "password": password},
        {"email": email, "username": username, "display_name": display_name, "password": password},
    ]
    login_json_payloads = [
        {"email": email, "password": password},
        {"email": email, "username": username, "password": password},
        {"username": username, "password": password},
    ]

    register_ok = False
    register_detail = "register endpoint not found"
    register_token: str | None = None
    for path in register_paths:
        for payload in register_payloads:
            response = client.post(f"{BASE_HTTP_URL}{path}", json=payload)
            if response.status_code in {200, 201, 202, 409}:
                register_ok = True
                register_detail = f"{path} -> {response.status_code}"
                if response.headers.get("content-type", "").startswith("application/json"):
                    body = response.json()
                    register_token = body.get("access_token") or body.get("token") or body.get("jwt")
                break
            if response.status_code not in {404, 405, 415, 422}:
                register_detail = f"{path} -> {response.status_code}"
        if register_ok:
            break

    if not register_ok:
        return CheckResult("register+login", False, register_detail), None

    token: str | None = register_token
    login_ok = False
    login_detail = "login endpoint not found"
    for path in login_paths:
        for payload in login_json_payloads:
            response = client.post(f"{BASE_HTTP_URL}{path}", json=payload)
            if response.status_code == 200:
                login_ok = True
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                token = body.get("access_token") or body.get("token") or body.get("jwt") or token
                login_detail = f"{path} -> 200"
                break
            if response.status_code not in {404, 405, 415, 422}:
                login_detail = f"{path} -> {response.status_code}"
        if login_ok:
            break

    if not login_ok:
        for path in login_paths:
            for payload in login_json_payloads:
                response = client.post(f"{BASE_HTTP_URL}{path}", data=payload)
                if response.status_code == 200:
                    login_ok = True
                    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    token = body.get("access_token") or body.get("token") or body.get("jwt") or token
                    login_detail = f"{path} (form) -> 200"
                    break
                if response.status_code not in {404, 405, 415, 422}:
                    login_detail = f"{path} -> {response.status_code}"
            if login_ok:
                break

    return CheckResult("register+login", login_ok, login_detail), token


def check_tools(client: httpx.Client, openapi_spec: dict[str, Any], token: str | None) -> CheckResult:
    if not token:
        return CheckResult("tools", False, "no auth token available for /api/tools")

    tool_paths = find_paths(openapi_spec, "tools", method="get") or ["/api/tools"]
    headers = {"Authorization": f"Bearer {token}"}

    for path in tool_paths:
        response = client.get(f"{BASE_HTTP_URL}{path}", headers=headers)
        if response.status_code == 200:
            return CheckResult("tools", True, f"{path} -> 200")
        if response.status_code not in {404, 405}:
            return CheckResult("tools", False, f"{path} -> {response.status_code}")

    return CheckResult("tools", False, "tools endpoint not found")


async def _check_websocket_async(token: str | None) -> CheckResult:
    if websockets is None:
        return CheckResult("websocket", False, "websockets package not installed")
    if not token:
        return CheckResult("websocket", False, "no auth token available for websocket auth")

    candidates = parse_ws_paths_from_static() + [
        "/ws",
        "/ws/chat",
        "/ws/stream",
        "/api/ws",
        "/api/ws/chat",
    ]
    tried: list[str] = []

    for path in dict.fromkeys(candidates):
        if not path.startswith("/"):
            continue

        ws_url = f"ws://{HOST}:{PORT}{path}"
        tried.append(ws_url)

        try:
            async with websockets.connect(ws_url, open_timeout=5, close_timeout=2):  # type: ignore[attr-defined]
                pass
        except Exception:  # noqa: BLE001
            continue

        try:
            async with websockets.connect(ws_url, open_timeout=5, close_timeout=2) as ws:  # type: ignore[attr-defined]
                await ws.send(json.dumps({"text": "smoke websocket ping", "token": token}))
                response = await asyncio.wait_for(ws.recv(), timeout=12)
                if response:
                    return CheckResult("websocket", True, f"connected to {path}")
        except Exception:  # noqa: BLE001
            continue

    return CheckResult("websocket", False, f"unable to connect ({len(tried)} attempts)")


def check_websocket(token: str | None) -> CheckResult:
    return asyncio.run(_check_websocket_async(token))


def start_server() -> subprocess.Popen[str]:
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    env = os.environ.copy()
    env.setdefault("HOST", HOST)
    env.setdefault("PORT", str(PORT))
    env.setdefault("ANTHROPIC_API_KEY", "sk-ant-smoke-test")
    env.setdefault("BIOMNI_SKIP_DATALAKE", "true")

    return subprocess.Popen(  # noqa: S603
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_server(proc: subprocess.Popen[str]) -> str:
    output = ""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if proc.stdout is not None:
        output = proc.stdout.read()
    return output


def print_result(result: CheckResult) -> None:
    status = "PASS" if result.ok else "FAIL"
    print(f"[{status}] {result.name}: {result.detail}")


def main() -> int:
    proc = start_server()
    results: list[CheckResult] = []

    try:
        ready, detail = wait_for_server()
        if not ready:
            results.append(CheckResult("startup", False, detail))
            for result in results:
                print_result(result)
            return 1

        with httpx.Client(timeout=15.0) as client:
            health_response = client.get(f"{BASE_HTTP_URL}/api/health")
            results.append(CheckResult("health", health_response.status_code == 200, f"/api/health -> {health_response.status_code}"))

            openapi_spec = try_get_openapi(client)

            auth_result, token = check_auth(client, openapi_spec)
            results.append(auth_result)

            tools_result = check_tools(client, openapi_spec, token)
            results.append(tools_result)

            ws_result = check_websocket(token)
            results.append(ws_result)
    finally:
        logs = stop_server(proc)

    for result in results:
        print_result(result)

    success = all(result.ok for result in results)
    if not success and logs.strip():
        print("\n--- server logs (tail) ---")
        print("\n".join(logs.strip().splitlines()[-40:]))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
