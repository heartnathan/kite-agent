"""Agent 工具（function calling）：網路搜尋、計算器、檔案讀寫（沙盒）。

所有檔案操作限制在 DATA_DIR/agent_workspace/ 內。
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
import time

import httpx

DATA_DIR = os.environ.get("DATA_DIR", "./data")
WORKSPACE = os.path.join(os.path.abspath(DATA_DIR), "agent_workspace")
MAX_RESULT_CHARS = 12000
MAX_FILE_BYTES = 1 * 1024 * 1024  # 讀回內容上限 1MB


def ensure_workspace():
    os.makedirs(WORKSPACE, exist_ok=True)


# ---------------------------------------------------------------- 搜尋

_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


def _clean_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<br\s*/?>", "\n", html)
    html = re.sub(r"(?s)</(p|div|h[1-6]|li|tr)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                 .replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def search_web(query: str, max_results: int = 5) -> dict:
    """用 DuckDuckGo HTML 端點搜尋（免費、免金鑰）。"""
    if not query or not query.strip():
        return {"ok": False, "error": "query 不能為空"}
    max_results = max(1, min(int(max_results or 5), 10))
    try:
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_SEARCH_HEADERS,
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"搜尋請求失敗：{exc}"}

    # 結果結構：<a class="result__a" href="...">title</a>
    #             <a class="result__snippet" ...>snippet</a>
    title_re = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.S,
    )
    snip_re = re.compile(
        r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
        re.S,
    )
    titles = title_re.findall(html)
    snippets = snip_re.findall(html)

    # href 可能是 //duckduckgo.com/l/?uddg=<urlencoded> 或直連
    def real_url(href: str) -> str:
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                from urllib.parse import unquote
                return unquote(m.group(1))
        return href

    results = []
    for i, (url, title) in enumerate(titles[:max_results]):
        snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
        results.append({
            "title": _clean_html(title)[:300],
            "url": real_url(url)[:500],
            "snippet": snippet[:500],
        })
    if not results:
        return {"ok": True, "query": query, "results": [],
                "note": "沒有找到結果，可換關鍵字再試"}
    return {"ok": True, "query": query, "results": results}


# ---------------------------------------------------------------- 計算

_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_ALLOWED_UNARY = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}
_SAFE_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "log": math.log, "log10": math.log10,
    "exp": math.exp, "abs": abs, "round": round, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _SAFE_FUNCS:
        if node.keywords:
            raise ValueError("此函數不接受具名參數")
        args = [_eval_node(a) for a in node.args]
        return _SAFE_FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name) and node.id in ("pi", "e"):
        return math.pi if node.id == "pi" else math.e
    raise ValueError(f"不支援的運算：{ast.dump(node)[:80]}")


def calculator(expression: str) -> dict:
    """安全計算器：支援 + - * / // % **、括號、sqrt/sin/cos/log/exp 等。"""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        value = _eval_node(tree)
        if isinstance(value, float) and math.isinf(value):
            return {"ok": False, "error": "結果為無窮大"}
        if isinstance(value, float) and abs(value) > 1e15:
            return {"ok": False, "error": "結果數值過大"}
        return {"ok": True, "expression": expression, "result": value}
    except ZeroDivisionError:
        return {"ok": False, "error": "除以零"}
    except Exception as exc:
        return {"ok": False, "error": f"無法計算：{exc}"}


# ---------------------------------------------------------------- 檔案

def _safe_path(rel: str) -> str:
    ensure_workspace()
    p = os.path.normpath(os.path.join(WORKSPACE, rel or ""))
    if p != WORKSPACE and not p.startswith(WORKSPACE + os.sep):
        raise PermissionError("路徑超出沙盒範圍")
    return p


def list_files(path: str = ".") -> dict:
    """列出沙盒資料夾內的檔案。"""
    try:
        base = _safe_path(path)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    if not os.path.exists(base):
        return {"ok": False, "error": f"路徑不存在：{path}"}
    if os.path.isfile(base):
        st = os.stat(base)
        return {"ok": True, "files": [
            {"name": os.path.basename(base), "size": st.st_size}]}
    entries = []
    try:
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            st = os.stat(full)
            entries.append({
                "name": name,
                "type": "dir" if os.path.isdir(full) else "file",
                "size": st.st_size if os.path.isfile(full) else None,
            })
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": path, "files": entries[:200]}


def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> dict:
    """讀取沙盒內的文字檔（可分頁）。"""
    try:
        full = _safe_path(path)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    if not os.path.isfile(full):
        return {"ok": False, "error": f"檔案不存在：{path}"}
    size = os.path.getsize(full)
    if size > MAX_FILE_BYTES:
        return {"ok": False, "error": f"檔案過大（{size} bytes > {MAX_FILE_BYTES}）"}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    start_line = max(1, int(start_line or 1))
    max_lines = max(1, min(int(max_lines or 400), 2000))
    chunk = lines[start_line - 1: start_line - 1 + max_lines]
    numbered = "\n".join(
        f"{i + start_line:>5}| {line.rstrip()}" for i, line in enumerate(chunk))
    return {
        "ok": True, "path": path,
        "total_lines": len(lines),
        "returned_lines": f"{start_line}~{start_line + len(chunk) - 1}",
        "content": numbered[:MAX_RESULT_CHARS],
    }


def write_file(path: str, content: str, append: bool = False) -> dict:
    """寫入沙盒內的檔案（覆寫或附加）。"""
    try:
        full = _safe_path(path)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return {"ok": False, "error": "內容過大（>1MB）"}
    try:
        os.makedirs(os.path.dirname(full) or WORKSPACE, exist_ok=True)
        mode = "a" if append else "w"
        with open(full, mode, encoding="utf-8") as f:
            n = f.write(content)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": path, "bytes_written": n,
            "mode": "append" if append else "write"}


# ---------------------------------------------------------------- 註冊

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "用 DuckDuckGo 搜尋網路，回傳標題/網址/摘要。"
                           "需要最新資訊、事實查核時使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜尋關鍵字"},
                    "max_results": {"type": "integer",
                                    "description": "結果數量 1-10，預設 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "精確計算數學運算式（+ - * / // % **、括號、"
                          "sqrt/sin/cos/log/log10/exp/abs/round/pi/e）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string",
                                   "description": "運算式，例如 (2+3)*12.5"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出沙盒資料夾中的檔案與大小。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相對於沙盒的路徑，預設 '.'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "讀取沙盒內的文字檔，回傳帶行號的內容（可分頁）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相對於沙盒的路徑"},
                    "start_line": {"type": "integer", "description": "起始行，預設 1"},
                    "max_lines": {"type": "integer", "description": "最多行數，預設 400"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "寫入沙盒內的檔案（覆寫或附加）。適合產生報告、資料檔、程式碼。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相對於沙盒的路徑"},
                    "content": {"type": "string", "description": "檔案內容"},
                    "append": {"type": "boolean", "description": "true=附加到結尾"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

_TOOL_IMPLS = {
    "search_web": lambda **kw: search_web(**kw),
    "calculator": lambda **kw: calculator(**kw),
    "list_files": lambda **kw: list_files(**kw),
    "read_file": lambda **kw: read_file(**kw),
    "write_file": lambda **kw: write_file(**kw),
}


def run_tool(name: str, arguments: dict) -> str:
    """執行工具，一律回傳 JSON 字串（給 LLM 看的 tool 訊息）。"""
    fn = _TOOL_IMPLS.get(name)
    if fn is None:
        return json.dumps({"ok": False, "error": f"未知工具：{name}"},
                          ensure_ascii=False)
    started = time.time()
    try:
        result = fn(**(arguments or {}))
    except TypeError as exc:
        result = {"ok": False, "error": f"參數錯誤：{exc}"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"工具執行失敗：{exc}"}
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[: MAX_RESULT_CHARS - 50] + ' ...}(已截斷)"'
    return text
