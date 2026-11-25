"""Agent 執行核心：串接 LLM、工具、資料庫。

run_agent_stream(db, agent, session_id, user_message) 為主要入口：
同步生成器，逐個 yield SSE 事件 dict：
  message_saved / assistant_start / delta / tool_call / tool_result /
  done / error
多輪 tool loop：LLM 呼叫工具 → 執行 → 回傳結果 → 再問，直到最終回答
或達 max_iterations。
"""
from __future__ import annotations

import json

import llm as llm_mod
import tools as tools_mod
from db import DB


def build_messages(db: DB, agent: dict, session_id: str) -> list[dict]:
    """組裝給 LLM 的訊息：system prompt + 歷史（含 tool call / tool 訊息）。"""
    msgs: list[dict] = []
    if agent.get("system_prompt"):
        msgs.append({"role": "system", "content": agent["system_prompt"]})
    for m in db.list_messages(session_id):
        role = m["role"]
        if role == "tool":
            msgs.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or m["id"],
                "content": m["content"],
            })
        elif role == "assistant" and m.get("tool_name"):
            msgs.append({
                "role": "assistant",
                "content": m["content"] or None,
                "tool_calls": [{
                    "id": m.get("tool_call_id") or m["id"],
                    "type": "function",
                    "function": {
                        "name": m["tool_name"],
                        "arguments": json.dumps(m.get("tool_input") or {},
                                                ensure_ascii=False),
                    },
                }],
            })
        elif role in ("user", "assistant"):
            msgs.append({"role": role, "content": m["content"]})
    return msgs


def resolve_default(db: DB) -> tuple[dict | None, str]:
    """解析「預設供應商 + 預設模型」（全部存在 DB，由設定面板管理）。

    回傳 (provider_dict | None, model_id)。未設定時 provider 為 None。
    """
    d = db.get_default()
    provider_id, model = d.get("provider_id", ""), d.get("model", "")
    provider = db.get_provider(provider_id) if provider_id else None
    if provider is None and provider_id:
        # 預設供應商被刪了
        provider = None
    if provider is None:
        # 尚未設定：給出最清楚的錯誤
        providers = db.list_providers()
        if not providers:
            return None, ""
        provider = providers[0]
    return provider, model


def run_agent_stream(db: DB, agent: dict, session_id: str,
                     user_message: str):
    """同步生成器。SSE 轉送層（web.py）把每個 dict 包成 data: 行。"""
    tools_mod.ensure_workspace()

    user_msg = db.add_message(session_id, "user", user_message)
    yield {"type": "message_saved", "id": user_msg["id"], "role": "user",
           "content": user_message}

    provider, model = resolve_default(db)
    if provider is None:
        yield {"type": "error",
               "error": "尚未設定 LLM 供應商：請到左下角「🔌」開啟模型設定,新增供應商"}
        return
    if not model:
        yield {"type": "error",
               "error": "尚未選擇預設模型：請到左下角「🔌」拉取模型並設為預設"}
        return

    max_iter = max(1, int(agent.get("max_iterations") or 8))
    temperature = agent.get("temperature")
    max_tokens = agent.get("max_tokens")

    for iteration in range(max_iter):
        messages = build_messages(db, agent, session_id)

        # ---- 問 LLM（流式）----
        round_text = ""
        round_calls: list[dict] = []
        error_text = None
        try:
            for ev in llm_mod.chat_stream(
                provider, model, messages, tools=tools_mod.TOOLS,
                temperature=temperature, max_tokens=max_tokens,
            ):
                if ev["type"] == "delta":
                    round_text += ev["content"]
                    yield {"type": "delta", "content": ev["content"]}
                elif ev["type"] == "tool_calls":
                    round_calls = ev["tool_calls"]
        except llm_mod.LLMError as exc:
            error_text = str(exc)
        except Exception as exc:  # noqa: BLE001
            error_text = f"內部錯誤：{exc}"

        if error_text is not None:
            if round_text:
                db.add_message(session_id, "assistant", round_text,
                               status="error")
            db.add_message(session_id, "assistant", f"⚠️ {error_text}",
                           status="error")
            yield {"type": "error", "error": error_text}
            return

        # ---- 沒有 tool call → 最終回答 ----
        if not round_calls:
            if not round_text:
                # 模型空回：存個提示後結束
                db.add_message(session_id, "assistant",
                               "（模型沒有回傳內容）", status="empty")
            else:
                db.add_message(session_id, "assistant", round_text)
            db.touch_session(session_id)
            _auto_title(db, session_id, user_message)
            yield {"type": "done", "content": round_text}
            return

        # ---- 有 tool call：先存 assistant 訊息，再逐一執行 ----
        # 若同一輪既有文字又有 tool call，文字掛在「第一個」call 訊息上
        first = True
        assistant_content = round_text or None
        for call in round_calls:
            content = assistant_content if first else ""
            db.add_message(
                session_id, "assistant", content or "",
                tool_call_id=call["id"],
                tool_name=call["name"],
                tool_input=call["arguments"],
            )
            first = False
            yield {"type": "tool_call", "id": call["id"],
                   "name": call["name"],
                   "arguments": call["arguments"]}

            result_text = tools_mod.run_tool(call["name"],
                                              call["arguments"] or {})
            db.add_message(
                session_id, "tool", result_text,
                tool_call_id=call["id"], tool_name=call["name"],
            )
            yield {"type": "tool_result", "id": call["id"],
                   "name": call["name"],
                   "output": result_text[:4000]}
        # 繼續下一輪，讓 LLM 根據工具結果繼續
    # 超過最大輪數
    db.add_message(session_id, "assistant",
                   f"（已達到最大工具輪數 {max_iter}，停止）", status="stopped")
    db.touch_session(session_id)
    yield {"type": "done",
           "content": f"（已達到最大工具輪數 {max_iter}，停止）"}


def _auto_title(db: DB, session_id: str, user_message: str):
    session = db.get_session(session_id)
    if session and not session.get("title"):
        title = user_message.strip().replace("\n", " ")[:40]
        if title:
            db.update_session_title(session_id, title)
