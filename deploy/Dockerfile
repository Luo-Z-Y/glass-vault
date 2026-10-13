# The Glass Vault demo server as hosted for judges (deploy/README.md). Build from the repo root:
#   docker build -f deploy/Dockerfile .
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, from the lock file, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# No root, and nothing in the image is writable by the server.
RUN useradd --system --uid 10001 --no-create-home glassvault
USER glassvault

# Inside the container it listens on every interface; only the reverse proxy reaches it.
ENV PATH=/app/.venv/bin:$PATH \
    GV_HOST=0.0.0.0 \
    GV_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["python", "-m", "glassvault.web.server"]
