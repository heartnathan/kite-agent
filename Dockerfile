FROM python:3.11-slim

LABEL org.opencontainers.image.title="Kite Agent" \
      org.opencontainers.image.description="Single-port AI agent web app (web UI + REST API + health check)" \
      org.opencontainers.image.authors="heartnathan" \
      org.opencontainers.image.source="https://github.com/heartnathan/kite-agent" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000 \
    HOST=0.0.0.0 \
    DATA_DIR=/data

# 對外唯一埠號
EXPOSE 8000

# 翼龍面板健康檢查可用此端點
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:'+(__import__('os').environ.get('PORT') or '8000')+'/api/health')" || exit 1

CMD ["python", "web.py"]
