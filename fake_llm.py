"""假的 OpenAI 相容伺服器,用來端到端驗證設定面板流程(不進 docker image)。"""
import json, time
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()

MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4.5-preview", "o3-mini",
          "deepseek-chat", "deepseek-reasoner", "qwen2.5-72b-instruct",
          "llama-3.3-70b-versatile", "text-embedding-3-small"]


@app.get("/v1/models")
def models(request: Request):
    if request.headers.get("authorization") == "Bearer bad":
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if not body.get("stream"):
        return {"id": "c1", "model": body.get("model"), "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "pong"},
             "finish_reason": "stop"}], "usage": {"total_tokens": 2}}

    def gen():
        for piece in ["你好", ",我是", "假模型。"]:
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            time.sleep(0.02)
        yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")