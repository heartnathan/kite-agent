"""LLM 客戶端：OpenAI 相容 Chat Completions（流式 + tool calls + 模型列表）。

不內建任何供應商、不讀取任何 LLM 環境變數。
供應商（base_url / api_key）全部存在資料庫 providers 表，
由網頁「設定面板」新增／管理；model 由使用者拉取模型列表後挑選。
"""
from __future__ import annotations

import json

import httpx

DEFAULT_TIMEOUT = 300.0


class LLMConfigError(Exception):
    pass


class LLMError(Exception):
    def __init__(self, message: str, status_code: int | None = None,
                 body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _cfg_from_provider(provider: dict, timeout: float | None = None) -> dict:
    """把 providers 表的一列變成呼叫設定。"""
    base_url = (provider.get("base_url") or "").strip().rstrip("/")
    api_key = (provider.get("api_key") or "").strip()
    if not base_url:
        raise LLMConfigError("供應商未設定 API 位址（base_url）")
    return {
        "base_url": base_url,
        "api_key": api_key,
        "timeout": float(timeout or provider.get("timeout") or DEFAULT_TIMEOUT),
    }


def list_models(provider: dict,
                timeout: float | None = None) -> list[str]:
    """拉取供應商 /v1/models，回傳模型 id 清單（由新到舊排序前 500 筆）。"""
    cfg = _cfg_from_provider(provider, timeout)
    url = f"{cfg['base_url']}/models"
    try:
        resp = httpx.get(url, headers=_headers(cfg["api_key"]),
                         timeout=cfg["timeout"])
    except httpx.HTTPError as exc:
        raise LLMError(f"連線供應商失敗：{exc}") from exc
    if resp.status_code >= 400:
        raise LLMError(f"拉取模型失敗（HTTP {resp.status_code}）："
                       f"{resp.text[:300]}",
                       resp.status_code, resp.text[:1000])
    data = resp.json()
    items = data.get("data") or data.get("models") or []
    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    return sorted(set(ids))[:500]


def chat_stream(
    provider: dict,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
):
    """同步生成器：逐塊 yield SSE 事件 dict。

    事件格式（與 SSE 對接層一致）：
      {"type": "delta", "content": "..."}
      {"type": "tool_calls", "tool_calls": [{id, name, arguments(dict)}]}
      {"type": "finish", "usage": {...}, "finish_reason": "..."}
    錯誤時 raise LLMError。
    """
    cfg = _cfg_from_provider(provider, timeout)
    if not model:
        raise LLMConfigError("尚未選擇模型：請在設定面板拉取模型並設為預設")
    url = f"{cfg['base_url']}/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    with httpx.Client(timeout=cfg["timeout"], headers=_headers(cfg["api_key"])) as client:
        with client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "replace")
                raise LLMError(f"LLM API 錯誤（{resp.status_code}）："
                               f"{body[:300]}", resp.status_code, body[:1000])
            content_parts: list[str] = []
            # tool call 需要跨 chunk 累積：index -> {id, name, args_parts}
            tc_buf: dict[int, dict] = {}
            finish_reason = None
            usage = None
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    yield {"type": "delta", "content": piece}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tc_buf.setdefault(idx, {"id": "", "name": "",
                                                   "args": []})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"].append(fn["arguments"])
            # 結束時送出累積的 tool calls
            if tc_buf:
                calls = []
                for idx in sorted(tc_buf):
                    slot = tc_buf[idx]
                    raw_args = "".join(slot["args"]).strip()
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args = {"_raw": raw_args}
                    calls.append({
                        "id": slot["id"] or f"call_{idx}",
                        "name": slot["name"],
                        "arguments": args,
                    })
                yield {"type": "tool_calls", "tool_calls": calls}
            yield {"type": "finish", "finish_reason": finish_reason,
                   "usage": usage}


def chat_once(
    provider: dict,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> dict:
    """非流式一問一答。回傳 {"content", "tool_calls", "usage", "finish_reason"}"""
    cfg = _cfg_from_provider(provider, timeout)
    if not model:
        raise LLMConfigError("尚未選擇模型：請在設定面板拉取模型並設為預設")
    url = f"{cfg['base_url']}/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        resp = httpx.post(url, json=payload,
                          timeout=cfg["timeout"], headers=_headers(cfg["api_key"]))
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM 連線失敗：{exc}") from exc
    if resp.status_code >= 400:
        body = resp.text
        try:
            msg = resp.json().get("error", {}).get("message")
            if msg:
                body = msg
        except Exception:
            pass
        raise LLMError(f"LLM API 錯誤（{resp.status_code}）：{body[:300]}",
                       resp.status_code, body[:1000])
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": args,
        })
    return {
        "content": msg.get("content") or "",
        "tool_calls": tool_calls,
        "usage": data.get("usage"),
        "finish_reason": choice.get("finish_reason"),
        "model": data.get("model"),
    }
