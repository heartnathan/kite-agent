"""資料庫層：libsql（Turso 遠端 或 本機 SQLite），兩種模式。

環境變數：
- DATABASE_URL + TURSO_AUTH_TOKEN → Turso 遠端
- 否則 LIBSQL_PATH（預設 data/local.db）→ 本機 SQLite
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import libsql

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    avatar TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    temperature REAL DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 2048,
    max_iterations INTEGER DEFAULT 8,
    is_builtin INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    title TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_output TEXT,
    status TEXT DEFAULT 'ok',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    timeout REAL DEFAULT 300,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 設定鍵
SETTING_DEFAULT_PROVIDER = "default_provider"
SETTING_DEFAULT_MODEL = "default_model"

DEFAULT_AGENT = {
    "name": "助手",
    "description": "預設 AI 助手：可以對話、用網路搜尋、算數、讀寫檔案",
    "avatar": "🤖",
    "system_prompt": (
        "你是一個有實際能力的 AI 助理。你可以：\n"
        "1. 使用 search_web 工具搜尋網路取得最新資訊（答案要附上來源）。\n"
        "2. 使用 calculator 工具做精確計算。\n"
        "3. 使用 read_file / write_file 在沙盒資料夾中讀寫檔案。\n"
        "回答用使用者使用的語言（預設繁體中文），簡潔、有條理；"
        "需要多步驟時就動手做，不要只說你「會做」。"
    ),
    "temperature": 0.7,
    "max_tokens": 2048,
    "max_iterations": 8,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class DB:
    def __init__(self):
        self._conn = None
        self._local_lock = threading.RLock()
        url = os.environ.get("DATABASE_URL", "").strip()
        token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        if url:
            self.url = url
            if not token:
                raise RuntimeError("設定 DATABASE_URL 時必須一併設定 TURSO_AUTH_TOKEN")
            self._conn = libsql.connect(url, auth_token=token)
            self.mode = "turso"
        else:
            path = os.environ.get("LIBSQL_PATH", "").strip() or os.path.join(
                os.environ.get("DATA_DIR", "./data"), "local.db"
            )
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.url = path
            self._conn = libsql.connect(path)
            self.mode = "local"
        self.init()

    # ---------- 基礎 ----------
    def init(self):
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            cur.execute(stmt)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    @contextmanager
    def _tx(self):
        """local 模式加鎖（單一寫者）；turso 模式直接跑。"""
        if self.mode == "local":
            with self._local_lock:
                try:
                    yield self._conn
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
        else:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params=()) -> list[dict]:
        with self._tx() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in (cur.description or [])]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def execute(self, sql: str, params=()) -> int:
        with self._tx() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur.rowcount

    # ---------- agents ----------
    def get_agent(self, agent_id: str) -> dict | None:
        rows = self.query("SELECT * FROM agents WHERE id = ?", (agent_id,))
        return rows[0] if rows else None

    def list_agents(self) -> list[dict]:
        return self.query(
            "SELECT * FROM agents ORDER BY is_builtin DESC, created_at ASC"
        )

    def create_agent(self, agent_id: str, data: dict) -> dict:
        now = _now()
        self.execute(
            """INSERT INTO agents
               (id, name, description, avatar, system_prompt, temperature,
                max_tokens, max_iterations, is_builtin, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,0,?,?)""",
            (
                agent_id,
                data["name"],
                data.get("description", ""),
                data.get("avatar", ""),
                data.get("system_prompt", ""),
                float(data.get("temperature", 0.7)),
                int(data.get("max_tokens", 2048)),
                int(data.get("max_iterations", 8)),
                now,
                now,
            ),
        )
        return self.get_agent(agent_id)

    def update_agent(self, agent_id: str, data: dict) -> dict | None:
        fields, params = [], []
        for k in ("name", "description", "avatar", "system_prompt",
                  "temperature", "max_tokens", "max_iterations"):
            if k in data and data[k] is not None:
                fields.append(f"{k} = ?")
                params.append(data[k])
        if not fields:
            return self.get_agent(agent_id)
        fields.append("updated_at = ?")
        params += [_now(), agent_id]
        self.execute(f"UPDATE agents SET {', '.join(fields)} WHERE id = ?", params)
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> bool:
        agent = self.get_agent(agent_id)
        if not agent or agent["is_builtin"]:
            return False
        self.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        return True

    # ---------- sessions ----------
    def create_session(self, session_id: str, agent_id: str, title: str = "") -> dict:
        now = _now()
        self.execute(
            "INSERT INTO sessions (id, agent_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (session_id, agent_id, title, now, now),
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        rows = self.query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self, agent_id: str | None = None) -> list[dict]:
        if agent_id:
            return self.query(
                "SELECT s.*, a.name AS agent_name, a.avatar AS agent_avatar "
                "FROM sessions s JOIN agents a ON a.id = s.agent_id "
                "WHERE s.agent_id = ? ORDER BY s.updated_at DESC LIMIT 200",
                (agent_id,),
            )
        return self.query(
            "SELECT s.*, a.name AS agent_name, a.avatar AS agent_avatar "
            "FROM sessions s JOIN agents a ON a.id = s.agent_id "
            "ORDER BY s.updated_at DESC LIMIT 200"
        )

    def update_session_title(self, session_id: str, title: str):
        self.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), session_id),
        )

    def touch_session(self, session_id: str):
        self.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )

    def delete_session(self, session_id: str):
        self.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # ---------- messages ----------
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_input=None,
        tool_output=None,
        status: str = "ok",
    ) -> dict:
        import json
        import time
        msg_id = f"msg_{os.urandom(10).hex()}"
        seq = time.time_ns()  # 單調遞增，保證訊息順序
        self.execute(
            """INSERT INTO messages
               (id, seq, session_id, role, content, tool_call_id, tool_name,
                tool_input, tool_output, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg_id,
                seq,
                session_id,
                role,
                content,
                tool_call_id,
                tool_name,
                json.dumps(tool_input, ensure_ascii=False) if tool_input is not None else None,
                tool_output,
                status,
                _now(),
            ),
        )
        return {
            "id": msg_id, "session_id": session_id, "role": role,
            "content": content, "tool_call_id": tool_call_id,
            "tool_name": tool_name, "tool_input": tool_input,
            "tool_output": tool_output, "status": status,
        }

    def list_messages(self, session_id: str) -> list[dict]:
        import json
        rows = self.query(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        )
        out = []
        for r in rows:
            if r.get("tool_input"):
                try:
                    r["tool_input"] = json.loads(r["tool_input"])
                except (ValueError, TypeError):
                    pass
            out.append(r)
        return out

    def delete_messages(self, session_id: str, after_id: str):
        """刪除某則訊息（含）之後的所有訊息（用於重新整理）。"""
        self.execute(
            "DELETE FROM messages WHERE session_id = ? AND id != ? "
            "AND created_at > (SELECT created_at FROM messages WHERE id = ?)",
            (session_id, after_id, after_id),
        )

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        rows = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str):
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------- providers（LLM 供應商，全由設定面板管理） ----------
    def list_providers(self) -> list[dict]:
        return self.query("SELECT * FROM providers ORDER BY created_at ASC")

    def get_provider(self, provider_id: str) -> dict | None:
        rows = self.query("SELECT * FROM providers WHERE id = ?", (provider_id,))
        return rows[0] if rows else None

    def create_provider(self, provider_id: str, data: dict) -> dict:
        now = _now()
        self.execute(
            "INSERT INTO providers (id, name, base_url, api_key, timeout, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (provider_id, data["name"], data["base_url"].rstrip("/"),
             data.get("api_key", ""), float(data.get("timeout") or 300),
             now, now),
        )
        return self.get_provider(provider_id)

    def update_provider(self, provider_id: str, data: dict) -> dict | None:
        fields, params = [], []
        for k in ("name", "base_url", "api_key", "timeout"):
            if k in data and data[k] is not None:
                fields.append(f"{k} = ?")
                params.append(data[k].rstrip("/") if k == "base_url" else data[k])
        if not fields:
            return self.get_provider(provider_id)
        fields.append("updated_at = ?")
        params += [_now(), provider_id]
        self.execute(
            f"UPDATE providers SET {', '.join(fields)} WHERE id = ?", params)
        return self.get_provider(provider_id)

    def delete_provider(self, provider_id: str) -> bool:
        if not self.get_provider(provider_id):
            return False
        self.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        # 若刪掉的是預設供應商，清空預設
        if self.get_setting(SETTING_DEFAULT_PROVIDER) == provider_id:
            self.set_setting(SETTING_DEFAULT_PROVIDER, "")
        return True

    # ---------- 預設模型 ----------
    def get_default(self) -> dict:
        """回傳 {"provider_id", "model"}（可能為空字串）。"""
        return {
            "provider_id": self.get_setting(SETTING_DEFAULT_PROVIDER, "") or "",
            "model": self.get_setting(SETTING_DEFAULT_MODEL, "") or "",
        }

    def set_default(self, provider_id: str, model: str):
        if provider_id is not None:
            self.set_setting(SETTING_DEFAULT_PROVIDER, provider_id)
        if model is not None:
            self.set_setting(SETTING_DEFAULT_MODEL, model)
