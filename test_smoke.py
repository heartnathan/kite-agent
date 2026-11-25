"""本地冒煙測試：tools / db / agent loop / web API。用 mock LLM，不需金鑰。"""
import os, sys, json, tempfile, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="agenttest_")
os.environ["DATA_DIR"] = TMP
os.environ["LIBSQL_PATH"] = os.path.join(TMP, "t.db")

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" " + extra if extra and not cond else ""))
    if not cond:
        fails.append(name)

# ---- tools ----
import tools
check("calc 基本", tools.calculator("(128*45+3600)/8")["result"] == 1170.0)
check("calc 函數", tools.calculator("sqrt(16)+2**3")["result"] == 12)
check("calc div0 攔截", not tools.calculator("1/0")["ok"])
check("未知工具", "未知工具" in tools.run_tool("nope", {}))
check("路徑越界封鎖", not json.loads(tools.run_tool("write_file", {"path": "../../x", "content": "x"}))["ok"])
check("寫檔", json.loads(tools.run_tool("write_file", {"path": "notes/a.txt", "content": "hi 翼鸢"}))["ok"])
d = json.loads(tools.run_tool("read_file", {"path": "notes/a.txt"}))
check("讀檔", d["ok"] and "hi 翼鸢" in d["content"])
check("列檔", json.loads(tools.run_tool("list_files", {"path": "notes"}))["files"][0]["name"] == "a.txt")
sr = tools.search_web("台北 天氣")
check("search 結構", sr.get("ok") is not None and ("results" in sr or "error" in sr),
      json.dumps(sr, ensure_ascii=False)[:100])

# ---- db ----
from db import DB, DEFAULT_AGENT
db = DB()
db.create_agent("builtin", DEFAULT_AGENT)
db.execute("UPDATE agents SET is_builtin = 1 WHERE id='builtin'")
db.create_agent("a2", {**DEFAULT_AGENT, "name": "客服", "avatar": "🎧"})
check("建 agent", db.get_agent("a2") is not None)
db.create_session("s1", "builtin")
db.add_message("s1", "user", "你好")
db.add_message("s1", "assistant", "hello", tool_call_id="c1", tool_name="calculator", tool_input={"expression": "1+1"})
db.add_message("s1", "tool", '{"ok":true,"result":2}', tool_call_id="c1", tool_name="calculator")
msgs = db.list_messages("s1")
check("訊息順序", [m["role"] for m in msgs] == ["user", "assistant", "tool"])
check("tool_input 還原", msgs[1]["tool_input"] == {"expression": "1+1"})

# ---- provider / 預設模型(現在全部存在 DB,不再用環境變數)----
pv = db.create_provider("pv_test", {
    "name": "Mock", "base_url": "https://example.invalid/v1",
    "api_key": "sk-test", "timeout": 60,
})
check("建供應商", db.get_provider("pv_test") is not None and pv["name"] == "Mock")
check("無金鑰欄位預設", db.create_provider("pv_nokey", {
    "name": "Local", "base_url": "http://localhost:11434/v1"})["api_key"] == "")
db.set_default("pv_test", "mock-model")
check("設預設模型", db.get_default() == {"provider_id": "pv_test", "model": "mock-model"})
d = db.update_provider("pv_test", {"timeout": 120})
check("改供應商", d["timeout"] == 120)
db.update_provider("pv_test", {"timeout": 60})

# ---- agent loop (mock) ----
import llm
_orig_chat_stream = llm.chat_stream
from agent import build_messages, run_agent_stream
calls = {"n": 0}
def fake_stream(provider, model, messages, tools=None, **kw):
    calls["n"] += 1
    if calls["n"] == 1:
        yield {"type": "delta", "content": "讓我算一下。\n"}
        yield {"type": "tool_calls", "tool_calls": [{"id": "c1", "name": "calculator", "arguments": {"expression": "12*34"}}]}
        yield {"type": "finish"}
    else:
        yield {"type": "delta", "content": "答案是 408。"}
        yield {"type": "finish"}
llm.chat_stream = fake_stream
agent = db.get_agent("builtin")
events = list(run_agent_stream(db, agent, "s1", "12*34 等於幾"))
types = [e["type"] for e in events]
check("事件流", all(t in types for t in ("delta", "tool_call", "tool_result", "done")), str(types))
check("最終答案存檔", db.list_messages("s1")[-1]["content"] == "答案是 408。")
b = build_messages(db, agent, "s1")
check("含 tool_calls", any(m.get("tool_calls") for m in b if m["role"] == "assistant"))
check("含 tool 訊息", sum(1 for m in b if m["role"] == "tool") >= 1)

def boom(provider, model, messages, tools=None, **kw):
    raise llm.LLMError("LLM API 錯誤（500）：test")
    yield
llm.chat_stream = boom
events = list(run_agent_stream(db, db.get_agent("builtin"), "s1", "再問"))
check("錯誤路徑", events[-1]["type"] == "error", str(events[-1]))

# ---- web API (FastAPI TestClient) ----
import importlib
os.environ["PORT"] = "8123"
for m in list(sys.modules):
    if m in ("web",):
        del sys.modules[m]
from fastapi.testclient import TestClient
import web
app = web.app
c = TestClient(app)
r = c.get("/api/health"); check("health 200", r.status_code == 200 and r.json()["ok"])
check("llm_ready", r.json()["llm_ready"] is True, json.dumps(r.json(), ensure_ascii=False))
r = c.get("/api/agents"); check("列 agent", r.status_code == 200 and len(r.json()["agents"]) >= 2)
r = c.post("/api/agents", json={"name": "測試A", "system_prompt": "x"}); check("建 agent API", r.status_code == 201)
nid = r.json()["agent"]["id"]
r = c.post("/api/agents", json={"name": "測試A", "system_prompt": "x"}); check("重複名稱被擋", r.status_code == 400)
r = c.patch(f"/api/agents/{nid}", json={"temperature": 0.3}); check("改 agent API", r.json()["agent"]["temperature"] == 0.3)
r = c.delete(f"/api/agents/{nid}"); check("刪 agent API", r.status_code == 200)

# ---- 供應商 API ----
r = c.get("/api/providers")
check("列供應商", r.status_code == 200 and any(p["id"] == "pv_test" for p in r.json()["providers"]))
check("預設經 API 讀出", c.get("/api/default").json()["model"] == "mock-model")
r = c.post("/api/providers", json={"name": "第二批", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-x"})
check("新增供應商 API", r.status_code == 201)
newpv = r.json()["provider"]["id"]
r = c.post("/api/providers", json={"name": "壞的", "base_url": ""})
check("空 base_url 被擋", r.status_code == 422)
r = c.patch(f"/api/providers/{newpv}", json={"timeout": 90})
check("改供應商 API", r.json()["provider"]["timeout"] == 90)
r = c.put("/api/default", json={"provider_id": newpv, "model": "deepseek-chat"})
check("設預設 API", r.json() == {"provider_id": newpv, "model": "deepseek-chat"})
r = c.put("/api/default", json={"provider_id": "pv_nope", "model": "x"})
check("設不存在的供應商被擋", r.status_code == 404)
r = c.delete(f"/api/providers/{newpv}")
check("刪供應商 API", r.status_code == 200)
r = c.put("/api/default", json={"provider_id": "pv_test", "model": "mock-model"})
# 拉取模型失敗要有清楚的 502(example.invalid 解析不到)
r = c.post("/api/providers/pv_test/models")
check("拉模型失敗回 502", r.status_code == 502, str(r.status_code))

# 沒有預設模型、又拉不到模型列表 → 明確報錯,不得偷偷用硬編碼保底模型
c.put("/api/default", json={"provider_id": "pv_nokey", "model": ""})
r = c.post("/api/providers/pv_nokey/test")
tj = r.json()
check("無模型不硬編碼保底", r.status_code == 200 and tj["ok"] is False
      and "gpt-3.5" not in json.dumps(tj, ensure_ascii=False), json.dumps(tj, ensure_ascii=False))
check("無模型提示拉取模型", "拉取模型" in tj.get("detail", ""), tj.get("detail", ""))
c.put("/api/default", json={"provider_id": "pv_test", "model": "mock-model"})

# ---- llm 層:list_models / chat_stream 錯誤處理(monkeypatch httpx)----
class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload or {}, text
    def json(self): return self._p
    def read(self): return self.text.encode()
class _FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def stream(self, *a, **k): return self
    def __iter__(self): return iter([])
def fake_get(url, headers=None, timeout=None):
    return _FakeResp(200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})
_orig_get, _orig_client = llm.httpx.get, llm.httpx.Client
llm.httpx.get = fake_get
check("list_models 去重排序", llm.list_models({"base_url": "https://x/v1", "api_key": "k"}) == ["gpt-4o", "gpt-4o-mini"])
llm.httpx.get = lambda *a, **k: _FakeResp(401, {}, "bad key")
try:
    llm.list_models({"base_url": "https://x/v1", "api_key": "k"}); ok = False
except llm.LLMError as e:
    ok = e.status_code == 401 and "401" in str(e)
check("list_models 401 轉 LLMError", ok)
llm.httpx.get = _orig_get

llm.chat_stream = _orig_chat_stream
try:
    list(llm.chat_stream({"base_url": "https://x/v1"}, "", [])); ok = False
except llm.LLMConfigError:
    ok = True
check("空模型名報設定錯誤", ok)
try:
    list(llm.chat_stream({"base_url": ""}, "m", [])); ok = False
except llm.LLMConfigError:
    ok = True
check("空 base_url 報設定錯誤", ok)

r = c.post("/api/sessions", json={"agent_id": "builtin"}); check("建 session", r.status_code == 201)
sid = r.json()["session"]["id"]

# 用 mock 串流走完整 /api/chat
llm.chat_stream = fake_stream
calls["n"] = 0
r = c.post("/api/chat", json={"agent_id": "builtin", "session_id": sid, "message": "hi"})
check("chat 200 SSE", r.status_code == 200 and "text/event-stream" in r.headers.get("content-type", ""), str(r.status_code))
body = r.text
check("SSE 有 tool_call", "tool_call" in body, body[:200])
check("SSE 有 stream_end", "stream_end" in body)
r = c.get(f"/api/sessions/{sid}/messages")
mm = r.json()["messages"]
check("訊息端點回傳", any(m["role"] == "user" for m in mm) and any(m["role"] == "assistant" for m in mm), str(len(mm)))

r = c.get("/api/files/notes/a.txt"); check("沙盒檔案下載", r.status_code == 200 and "hi 翼鸢" in r.text)
r = c.get("/api/files/../../etc/passwd"); check("下載路徑封鎖", r.status_code in (403, 404))

r = c.get("/"); check("前端 200", r.status_code == 200 and "Kite Agent" in r.text)
check("首頁含 favicon link", 'rel="icon"' in r.text and "/favicon.svg" in r.text)
r = c.get("/favicon.svg")
check("favicon.svg 200", r.status_code == 200 and "image/svg+xml" in r.headers.get("content-type", ""))
check("favicon 是 SVG", r.text.lstrip().startswith("<svg") and "</svg>" in r.text)
r = c.get("/favicon.ico")
check("favicon.ico 相容", r.status_code == 200 and "<svg" in r.text)

print("\n" + ("=" * 40))
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print(f"ALL PASS  (tmp={TMP})")
