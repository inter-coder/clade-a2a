# Clade Relay — production image
# Build: docker build -t clade-relay -f relay/Dockerfile .
# (kontekst je root projekta, ne relay/)
FROM python:3.13-slim AS base

WORKDIR /app

# Deps prvo (cache layer)
RUN pip install --no-cache-dir \
    fastapi==0.115.* \
    "uvicorn[standard]==0.30.*" \
    pydantic==2.* \
    "redis[asyncio]==7.*"

# Relay code
COPY relay/ /app/relay/

# tokens.json se mount-uje sa hosta u runtime (NE u image — secret!)
# Vidi docker-compose.yml: volume binding na /app/relay/tokens.json

EXPOSE 7777

# Healthcheck — Docker svake 30s proverava /health, restart ako 3x padne
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7777/health', timeout=3)" || exit 1

CMD ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "7777", "--log-level", "info"]
