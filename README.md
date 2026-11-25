# Kite Agent 🪁

> 原名「翼鸢 Agent」。英文名 **Kite**(鸢),GitHub 倉庫:`heartnathan/kite-agent`。

一個完整的 AI agent web app：**單埠**部署，適合放在翼龍面板（1Panel）的容器環境。

同一個埠口同時提供：

- **網頁 UI**（`/`）— 對話介面，支援串流輸出、工具執行過程可展開
- **REST API**（`/api/*`）— agents、sessions、chat（SSE 串流）
- **健康檢查**（`/api/health`）— 給面板容器探測用

## 功能

| 功能 | 說明 |
|---|---|
| 💬 串流對話 | SSE 即時輸出，OpenAI 相容 API（OpenAI / DeepSeek / Moonshot / SiliconFlow / Ollama…） |
| 🔧 工具鏈（function calling） | `search_web`（DuckDuckGo 免金鑰）、`calculator`（安全 AST 計算器）、`list_files` / `read_file` / `write_file`（沙盒檔案，防路徑越界） |
| 🤖 多 Agent | 可建立多個 agent（各自 system prompt、temperature、工具輪數） |
| 🗂 多輪工具 loop | LLM 呼叫工具 → 執行 → 結果回灌 → 繼續，直到最終回答 |
| 📦 資料庫 | **Turso**（HTTP API 遠端，推薦，資料在雲端）或**本機 SQLite**（Python 內建 sqlite3），同一套程式碼，無原生套件，x86_64 / ARM64 皆可建置 |
| 🐳 部署 | Docker 單容器單埠，`/api/health` 已接 HEALTHCHECK |

## 快速開始（本機）

```bash
pip install -r requirements.txt
python web.py                          # http://localhost:8000
```

打開網頁後點左下角 **🔌** 開啟設定面板:

1. **+ 新增供應商** → 填名稱、API 位址(如 `https://api.openai.com/v1`)、API Key
2. **🧪 測試連線** 確認金鑰正確
3. **📥 拉取模型** → 用關鍵字篩選(例如 `gpt`、`deepseek`、`7b`)
4. 點選模型 **設為預設**

> 供應商與模型設定全部存在資料庫(providers 表),**不需任何 LLM 環境變數**,
> 部署後在網頁上設定即可。

跑測試(不需金鑰、不需網路,用 mock LLM):

```bash
python test_smoke.py
```

## 翼龍面板部署

1. **建立應用**：自訂容器 / 圖片構建，選本資料夾（含 `Dockerfile`），或用
   `docker compose up -d --build`。
2. **埠號**：只映射 **一個** 埠，例如 `8000:8000`（面板「外網埠」隨便挑，容器埠固定 8000）。
3. **環境變數**（面板 → 應用 → 環境變數，參照 `.env.example`）：

   ```
   PORT=8000
   HOST=0.0.0.0
   # Turso(推薦):
   DATABASE_URL=https://<db>.turso.io
   TURSO_AUTH_TOKEN=<token>
   # 或本機 SQLite(留空 DATABASE_URL 即可,自動用 /data/local.db):
   # LIBSQL_PATH=/data/local.db
   ```

4. **資料卷**：掛 `./data` → `/data`（本機 sqlite、agent 沙盒檔案、下載的檔案都在這）。
5. **健康檢查**：面板可設 `GET /api/health`（預期回傳 200 JSON）。
6. 打開 `http://<伺服器IP>:<外網埠>` 即可使用。

> 使用 Turso 時資料卷變得更重要（沙盒檔案仍需在 /data），但資料庫本身在雲端，
> 換機器只要帶同一組 `DATABASE_URL` + `TURSO_AUTH_TOKEN` 就能續用。

## API 摘要

| Method | Path | 說明 |
|---|---|---|
| GET | `/api/health` | 健康檢查（db 模式、llm 就緒狀態） |
| GET | `/api/agents` | 列出 agents |
| POST | `/api/agents` | 建立 agent |
| PATCH | `/api/agents/{id}` | 更新（部分欄位即可） |
| DELETE | `/api/agents/{id}` | 刪除（內建 agent 不可刪） |
| GET | `/api/sessions?agent_id=` | 列出對話 |
| POST | `/api/sessions` | 建立對話 |
| GET | `/api/sessions/{id}/messages` | 對話訊息（含工具執行紀錄） |
| POST | `/api/chat` | 發訊息 → **SSE 串流**回傳 |
| GET | `/api/files/{path}` | 下載 agent 沙盒內的檔案 |
| GET | `/api/providers` | 列出供應商 + 目前預設模型 |
| POST | `/api/providers` | 新增供應商(name / base_url / api_key / timeout) |
| PATCH | `/api/providers/{id}` | 更新供應商(部分欄位) |
| DELETE | `/api/providers/{id}` | 刪除供應商 |
| POST | `/api/providers/{id}/models` | 拉取該供應商的模型列表(GET /models) |
| POST | `/api/providers/{id}/test` | 用最小請求測試連線 |
| GET | `/api/default` | 讀取預設供應商 + 模型 |
| PUT | `/api/default` | 設定預設供應商 + 模型 |

`/api/chat` 請求：`{"agent_id": "...", "session_id": "可選", "message": "..."}`

SSE 事件：`message_saved` → `delta`（文字串流）→ `tool_call` → `tool_result` →（可能多輪）→ `done` → `stream_end`；失敗時 `error`。

## 檔案結構

```
web.py            FastAPI 主程式(單埠:UI + API + 健康檢查)
agent.py          agent 核心:訊息組裝、多輪工具 loop
llm.py            OpenAI 相容客戶端(流式 + tool calls 跨 chunk 合併)
tools.py          搜尋 / 計算器 / 沙盒檔案
db.py             Turso(HTTP API)或本機 SQLite(sqlite3)+ schema(含 providers / settings 表)
static/index.html 前端(無框架、內嵌 CSS/JS)
static/favicon.svg SVG 網站圖示(沿用主色漸層)
Dockerfile / docker-compose.yml / .env.example / requirements.txt
test_smoke.py     冒煙測試(mock LLM,不需金鑰、不需網路)
fake_llm.py       假的 OpenAI 相容伺服器(端到端測試設定面板用)
data/             執行時資料(本地 sqlite、agent 沙盒)
LICENSE           MIT 授權(© 2026 heartnathan)
```

## 授權

MIT License — 詳見 [LICENSE](LICENSE)。
