from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import aiosqlite  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - used only when dependency is unavailable
    class _FallbackCursor:
        def __init__(self, cursor: sqlite3.Cursor):
            self._cursor = cursor

        async def fetchone(self):
            return self._cursor.fetchone()

        async def fetchall(self):
            return self._cursor.fetchall()

        @property
        def rowcount(self) -> int:
            return self._cursor.rowcount

    class _FallbackConnection:
        def __init__(self, connection: sqlite3.Connection):
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FallbackCursor:
            return _FallbackCursor(self._connection.execute(sql, params))

        async def executescript(self, sql_script: str) -> None:
            self._connection.executescript(sql_script)

        async def commit(self) -> None:
            self._connection.commit()

        async def close(self) -> None:
            self._connection.close()

    class _FallbackAioSqlite:
        Row = sqlite3.Row
        IntegrityError = sqlite3.IntegrityError

        @staticmethod
        async def connect(path: str | Path) -> _FallbackConnection:
            conn = sqlite3.connect(path)
            return _FallbackConnection(conn)

    aiosqlite = _FallbackAioSqlite()  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "biomni.db"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def auto_title_from_message(content: str) -> str:
    normalized = " ".join(content.split()).strip()
    if not normalized:
        return "New conversation"
    return normalized[:60]


def _new_id() -> str:
    return str(uuid.uuid4())


def _new_share_token() -> str:
    return secrets.token_urlsafe(24)


@asynccontextmanager
async def get_connection() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    async with get_connection() as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                share_token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_share_token ON conversations(share_token);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            """
        )
        await conn.commit()


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


async def create_user(
    email: str,
    password_hash: str,
    display_name: str,
    role: str = "user",
) -> dict[str, Any]:
    user_id = _new_id()
    now = utc_now_iso()
    normalized_email = email.strip().lower()
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO users(id, email, password_hash, display_name, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, normalized_email, password_hash, display_name.strip(), role, now),
        )
        await conn.commit()
    user = await get_user_by_id(user_id)
    if not user:
        raise RuntimeError("Failed to load created user")
    return user


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    normalized_email = email.strip().lower()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM users WHERE email = ? LIMIT 1",
            (normalized_email,),
        )
        row = await cursor.fetchone()
    return _row_to_dict(row)


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM users WHERE id = ? LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
    return _row_to_dict(row)


async def create_conversation(user_id: str, title: str | None = None) -> dict[str, Any]:
    conversation_id = _new_id()
    now = utc_now_iso()
    share_token = _new_share_token()
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO conversations(id, user_id, title, share_token, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, user_id, title, share_token, now, now),
        )
        await conn.commit()
    conversation = await get_conversation_by_id(conversation_id, user_id=user_id)
    if not conversation:
        raise RuntimeError("Failed to load created conversation")
    return conversation


async def get_conversation_by_id(
    conversation_id: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    async with get_connection() as conn:
        if user_id:
            cursor = await conn.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ? LIMIT 1",
                (conversation_id, user_id),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM conversations WHERE id = ? LIMIT 1",
                (conversation_id,),
            )
        row = await cursor.fetchone()
    return _row_to_dict(row)


async def list_conversations_for_user(user_id: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                c.id,
                c.user_id,
                c.title,
                c.share_token,
                c.created_at,
                c.updated_at,
                COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def touch_conversation(conversation_id: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (utc_now_iso(), conversation_id),
        )
        await conn.commit()


async def update_conversation_title_if_empty(conversation_id: str, title: str) -> None:
    if not title:
        return
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE conversations
            SET title = ?, updated_at = ?
            WHERE id = ? AND (title IS NULL OR TRIM(title) = '')
            """,
            (title, utc_now_iso(), conversation_id),
        )
        await conn.commit()


async def add_message(
    conversation_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message_id = _new_id()
    now = utc_now_iso()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO messages(id, conversation_id, role, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, conversation_id, role, content, metadata_json, now),
        )
        await conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        if role == "user":
            auto_title = auto_title_from_message(content)
            await conn.execute(
                """
                UPDATE conversations
                SET title = COALESCE(NULLIF(TRIM(title), ''), ?)
                WHERE id = ?
                """,
                (auto_title, conversation_id),
            )
        await conn.commit()
    message = await get_message_by_id(message_id)
    if not message:
        raise RuntimeError("Failed to load created message")
    return message


async def get_message_by_id(message_id: str) -> dict[str, Any] | None:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM messages WHERE id = ? LIMIT 1",
            (message_id,),
        )
        row = await cursor.fetchone()
    return _row_to_dict(row)


async def list_messages_for_conversation(conversation_id: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            """,
            (conversation_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_conversation_with_messages_for_user(
    conversation_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    conversation = await get_conversation_by_id(conversation_id, user_id=user_id)
    if not conversation:
        return None
    messages = await list_messages_for_conversation(conversation_id)
    conversation["messages"] = messages
    return conversation


async def delete_conversation_for_user(conversation_id: str, user_id: str) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
        await conn.commit()
    return cursor.rowcount > 0


async def ensure_share_token_for_conversation(conversation_id: str, user_id: str) -> str | None:
    share_token = _new_share_token()
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE conversations
            SET share_token = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (share_token, utc_now_iso(), conversation_id, user_id),
        )
        await conn.commit()
    if cursor.rowcount <= 0:
        return None
    return share_token


async def get_shared_conversation(identifier: str) -> dict[str, Any] | None:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM conversations
            WHERE share_token = ? OR id = ?
            LIMIT 1
            """,
            (identifier, identifier),
        )
        row = await cursor.fetchone()
    conversation = _row_to_dict(row)
    if not conversation:
        return None
    messages = await list_messages_for_conversation(conversation["id"])
    conversation["messages"] = messages
    return conversation
