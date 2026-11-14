"""SQLite persistence for chats and messages (single file, easy to back up)."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import CHAT_DB, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    project TEXT DEFAULT '',
    pinned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '',
    tool_call_id TEXT DEFAULT '',
    name TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);
"""


class ChatStore:
    def __init__(self, path: Path | None = None):
        ensure_dirs()
        self.path = path or CHAT_DB
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    # ----------------------------------------------------------------- chats
    def create_chat(self, title: str = "New chat", project: str = "") -> dict:
        cid = uuid.uuid4().hex[:12]
        now = time.time()
        self._conn.execute(
            "INSERT INTO chats(id,title,created_at,updated_at,project) VALUES(?,?,?,?,?)",
            (cid, title, now, now, project))
        self._conn.commit()
        return self.get_chat(cid)

    def get_chat(self, chat_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        return dict(row) if row else None

    def list_chats(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS msg_count "
            "FROM chats c ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rename_chat(self, chat_id: str, title: str) -> None:
        self._conn.execute("UPDATE chats SET title=?, updated_at=? WHERE id=?",
                           (title, time.time(), chat_id))
        self._conn.commit()

    def delete_chat(self, chat_id: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        self._conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        self._conn.commit()

    def touch(self, chat_id: str, title: str | None = None) -> None:
        if title:
            self._conn.execute("UPDATE chats SET title=?, updated_at=? WHERE id=?",
                               (title, time.time(), chat_id))
        else:
            self._conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (time.time(), chat_id))
        self._conn.commit()

    # -------------------------------------------------------------- messages
    def add_message(self, chat_id: str, role: str, content: str = "",
                    tool_calls: list[dict] | None = None, tool_call_id: str = "",
                    name: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO messages(chat_id,role,content,tool_calls,tool_call_id,name,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (chat_id, role, content, json.dumps(tool_calls) if tool_calls else "",
             tool_call_id, name, time.time()))
        self.touch(chat_id)
        self._conn.commit()
        return cur.lastrowid

    def messages(self, chat_id: str, limit: int = 400) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id ASC", (chat_id,)).fetchall()
        out = []
        for r in rows:
            out.append(self._row_to_msg(dict(r)))
        return out[-limit:] if limit else out

    @staticmethod
    def _row_to_msg(r: dict) -> dict:
        m: dict[str, Any] = {"role": r["role"], "content": r["content"] or ""}
        if r["tool_calls"]:
            try:
                m["tool_calls"] = json.loads(r["tool_calls"])
            except Exception:
                pass
        if r["tool_call_id"]:
            m["tool_call_id"] = r["tool_call_id"]
        if r["name"]:
            m["name"] = r["name"]
        m["_id"] = r["id"]
        m["_ts"] = r["created_at"]
        return m

    def clear_messages(self, chat_id: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        self._conn.commit()

    def export_chat(self, chat_id: str) -> dict:
        chat = self.get_chat(chat_id) or {}
        chat["messages"] = [{k: v for k, v in m.items() if not k.startswith("_")}
                            for m in self.messages(chat_id)]
        return chat

    def import_chat(self, data: dict) -> str:
        cid = self.create_chat(data.get("title", "Imported chat"), data.get("project", ""))["id"]
        for m in data.get("messages", []):
            self.add_message(cid, m.get("role", "user"), m.get("content", ""),
                             m.get("tool_calls"), m.get("tool_call_id", ""), m.get("name", ""))
        return cid
