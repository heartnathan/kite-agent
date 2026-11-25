"""Kite Agent — 單埠 Web 應用。

同一個 PORT 提供：
  /           → 前端（內建網頁 UI）
  /api/*      → JSON API（agents / sessions / chat SSE）
  /api/health → 健康檢查（翼龍面板容器健康探測可用）

啟動：python web.py   （PORT / HOST 由環境變數決定，預設 8000）
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db as db_mod
import tools as tools_mod
from agent import run_agent_stream
import llm as llm_mod

# ---------------------------------------------------------------- 設定

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
WORKSPACE = os.path.join(DATA_DIR, "agent_workspace")

# ---------------------------------------------------------------- 初始化

app = FastAPI(title="Kite Agent", version="1.1.0")

if os.environ.get("CORS_ORIGIN"):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in os.environ["CORS_ORIGIN"].split(",")
                       if o.strip()],
        allow_methods=["*"], allow_headers=["*"],
    )

db = db_mod.DB()
tools_mod.ensure_workspace()


def _ensure_builtin_agent():
    """保證至少有一個內建 agent（id 固定：builtin）。"""
    if db.get_agent("builtin"):
        return
    now = time.time()
    d = db_mod.DEFAULT_AGENT
    db.create_agent(
        "builtin",
        {
            "name": d["name"], "description": d["description"],
            "avatar": d["avatar"], "system_prompt": d["system_prompt"],
            "temperature": d["temperature"], "max_tokens": d["max_tokens"],
            "max_iterations": d["max_iterations"],
        },
    )
    # 內建標記
    db.execute("UPDATE agents SET is_builtin = 1 WHERE id = 'builtin'")


_ensure_builtin_agent()


# ---------------------------------------------------------------- 前端

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    return FileResponse(os.path.join(BASE_DIR, "static", "favicon.svg"),
                        media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    """舊瀏覽器 / 直接打 /favicon.ico 時回同一顆 SVG。"""
    return FileResponse(os.path.join(BASE_DIR, "static", "favicon.svg"),
                        media_type="image/svg+xml")

# 靜態資源（css/js 若拆開可放這；目前內嵌在 index.html）
# app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
#           name="static")


# ---------------------------------------------------------------- 健康

@app.get("/api/health")
def health():
    providers = db.list_providers()
    default = db.get_default()
    has_default = bool(default["provider_id"] and default["model"])
    return {
        "ok": True,
        "service": "Kite Agent",
        "db": db.mode,
        "llm_ready": bool(providers and has_default),
        "llm_model": default["model"] if has_default else "",
        "providers": len(providers),
        "time": time.time(),
    }


# ---------------------------------------------------------------- Agents

class AgentIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    avatar: str = "🤖"
    system_prompt: str = ""
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(2048, ge=1, le=128000)
    max_iterations: int = Field(8, ge=1, le=25)


class AgentPatch(BaseModel):
    """PATCH 用：所有欄位選填，只更新送來的欄位。"""
    name: str | None = Field(None, min_length=1, max_length=64)
    description: str | None = None
    avatar: str | None = None
    system_prompt: str | None = None
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1, le=128000)
    max_iterations: int | None = Field(None, ge=1, le=25)


@app.get("/api/agents")
def list_agents():
    return {"agents": db.list_agents()}


@app.post("/api/agents", status_code=201)
def create_agent(body: AgentIn):
    try:
        a = db.create_agent(f"ag_{uuid.uuid4().hex[:12]}", body.model_dump())
    except Exception as exc:  # 名稱重複等
        raise HTTPException(400, str(exc))
    return {"agent": a}


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    a = db.get_agent(agent_id)
    if not a:
        raise HTTPException(404, "agent 不存在")
    return {"agent": a}


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentPatch):
    data = body.model_dump(exclude_unset=True)
    a = db.update_agent(agent_id, data)
    if not a:
        raise HTTPException(404, "agent 不存在")
    return {"agent": a}


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    if agent_id == "builtin":
        raise HTTPException(400, "內建 agent 不可刪除")
    if not db.delete_agent(agent_id):
        raise HTTPException(404, "agent 不存在")
    return {"ok": True}


# ---------------------------------------------------------------- Sessions

class SessionIn(BaseModel):
    agent_id: str
    title: str = ""


@app.get("/api/sessions")
def list_sessions(agent_id: str | None = None):
    return {"sessions": db.list_sessions(agent_id)}


@app.post("/api/sessions", status_code=201)
def create_session(body: SessionIn):
    if not db.get_agent(body.agent_id):
        raise HTTPException(404, "agent 不存在")
    s = db.create_session(f"se_{uuid.uuid4().hex[:12]}",
                          body.agent_id, body.title)
    return {"session": s}


@app.get("/api/sessions/{session_id}/messages")
def list_messages(session_id: str):
    if not db.get_session(session_id):
        raise HTTPException(404, "session 不存在")
    msgs = db.list_messages(session_id)
    # 前端只展示 user / assistant（不含純 tool call 的中間訊息）
    out = []
    pending_tool: dict | None = None
    for m in msgs:
        if m["role"] == "tool":
            if pending_tool is not None:
                pending_tool["tool_output"] = m["content"]
                out.append(pending_tool)
                pending_tool = None
            continue
        if m["role"] == "assistant" and m.get("tool_name"):
            if pending_tool is not None and out and \
                    out[-1]["id"] == m.get("tool_call_id", ""):
                continue  # 已合併
            entry = {
                "id": m["id"], "role": "assistant",
                "content": m["content"],
                "tool_name": m["tool_name"],
                "tool_input": m.get("tool_input"),
                "tool_output": None, "status": m.get("status"),
            }
            pending_tool = entry
            out.append(entry)
            continue
        if m["role"] in ("user", "assistant"):
            out.append({
                "id": m["id"], "role": m["role"], "content": m["content"],
                "status": m.get("status"),
                "created_at": m.get("created_at"),
            })
    return {"messages": out}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    db.delete_session(session_id)
    return {"ok": True}


# ---------------------------------------------------------------- Chat (SSE)

class ChatIn(BaseModel):
    agent_id: str
    session_id: str | None = None
    message: str = Field(..., min_length=1, max_length=20000)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(body: ChatIn):
    agent = db.get_agent(body.agent_id)
    if not agent:
        raise HTTPException(404, "agent 不存在")

    if body.session_id:
        session = db.get_session(body.session_id)
        if not session or session["agent_id"] != body.agent_id:
            raise HTTPException(404, "session 不存在或不屬於此 agent")
    else:
        session = db.create_session(f"se_{uuid.uuid4().hex[:12]}",
                                    body.agent_id)

    def event_stream():
        try:
            for ev in run_agent_stream(db, agent, session["id"],
                                       body.message):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "error": f"串流中斷：{exc}"})
        yield _sse({"type": "stream_end",
                    "session_id": session["id"]})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------- 供應商（Providers）
# 外接 API 全部存在資料庫，由設定面板管理：多個供應商、拉取/篩選模型、預設模型。

class ProviderIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., min_length=1, max_length=300)
    api_key: str = ""
    timeout: float | None = Field(None, ge=5, le=600)


class ProviderPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    base_url: str | None = Field(None, min_length=1, max_length=300)
    api_key: str | None = None
    timeout: float | None = Field(None, ge=5, le=600)


@app.get("/api/providers")
def list_providers():
    return {"providers": db.list_providers(), "default": db.get_default()}


@app.post("/api/providers", status_code=201)
def create_provider(body: ProviderIn):
    p = db.create_provider(f"pv_{uuid.uuid4().hex[:12]}", body.model_dump())
    # 第一個供應商自動設為預設
    if db.list_providers() and not db.get_default()["provider_id"]:
        db.set_default(p["id"], None)
    return {"provider": p}


@app.patch("/api/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderPatch):
    p = db.update_provider(provider_id, body.model_dump(exclude_unset=True))
    if not p:
        raise HTTPException(404, "供應商不存在")
    return {"provider": p}


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str):
    if not db.delete_provider(provider_id):
        raise HTTPException(404, "供應商不存在")
    return {"ok": True}


@app.post("/api/providers/{provider_id}/models")
def fetch_models(provider_id: str):
    """拉取該供應商的模型列表（GET /v1/models），前端可再自行篩選。"""
    p = db.get_provider(provider_id)
    if not p:
        raise HTTPException(404, "供應商不存在")
    try:
        models = llm_mod.list_models(p)
    except llm_mod.LLMError as exc:
        raise HTTPException(502, f"拉取模型失敗：{exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"拉取模型失敗：{exc}")
    if not models:
        raise HTTPException(502, "供應商沒有回傳任何模型")
    return {"provider_id": provider_id, "count": len(models), "models": models}


@app.post("/api/providers/{provider_id}/test")
def test_provider(provider_id: str):
    """用最小請求(1 個 token)測試供應商是否可用。

    模型優先序:此供應商的預設模型 → 模型列表第一個。
    兩者都取不到時明確報錯,不使用任何硬編碼的保底模型。
    """
    p = db.get_provider(provider_id)
    if not p:
        raise HTTPException(404, "供應商不存在")
    d = db.get_default()
    model = d["model"] if d["provider_id"] == provider_id else ""
    picked = "預設模型" if model else ""
    if not model:
        try:
            ms = llm_mod.list_models(p, timeout=30)
        except Exception:  # noqa: BLE001
            ms = []
        if ms:
            model, picked = ms[0], "模型列表第一個"
    if not model:
        return {"ok": False, "model": "", "detail":
                "此供應商沒有預設模型,也無法從 /models 取得模型列表。"
                "請先「📥 拉取模型」,選一個模型「設為預設」,再測試連線。"}
    try:
        r = llm_mod.chat_once(
            p, model,
            [{"role": "user", "content": "hi"}],
            max_tokens=1, timeout=30,
        )
    except llm_mod.LLMConfigError as exc:
        raise HTTPException(400, str(exc))
    except llm_mod.LLMError as exc:
        if exc.status_code == 404:
            return {"ok": False, "model": model, "detail":
                    f"端點連線正常,但模型 {model} 不存在。"
                    "請先用「拉取模型」選擇正確模型。"}
        if exc.status_code in (401, 403):
            return {"ok": False, "model": model, "detail":
                    f"金鑰無效或無權限(HTTP {exc.status_code}),請檢查 API Key。"}
        return {"ok": False, "model": model, "detail": str(exc)}
    return {"ok": True, "model": r.get("model") or model,
            "content": (r.get("content") or "")[:80], "picked": picked}


# ---------------------------------------------------------------- 預設模型

class DefaultIn(BaseModel):
    provider_id: str | None = None
    model: str | None = None


@app.get("/api/default")
def get_default():
    return db.get_default()


@app.put("/api/default")
def set_default(body: DefaultIn):
    if body.provider_id is not None and body.provider_id:
        if not db.get_provider(body.provider_id):
            raise HTTPException(404, "供應商不存在")
    db.set_default(body.provider_id, body.model)
    return db.get_default()


# 供 agent 使用的沙盒檔案下載（只限 workspace 內）
@app.get("/api/files/{path:path}")
def download_file(path: str):
    tools_mod.ensure_workspace()
    full = os.path.normpath(os.path.join(WORKSPACE, path))
    if not (full == WORKSPACE or full.startswith(WORKSPACE + os.sep)):
        raise HTTPException(403, "路徑超出沙盒")
    if not os.path.isfile(full):
        raise HTTPException(404, "檔案不存在")
    return FileResponse(full)


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    n_prov = len(db.list_providers())
    print(f"🚀 Kite Agent 啟動：http://{host}:{port}  "
          f"(db={db.mode}, providers={n_prov})")
    uvicorn.run(app, host=host, port=port, log_level="info")
