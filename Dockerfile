# VBR Pulse — optional container image (PLAN §4.2). The laptop path is still `uv run pulse`.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY openapi ./openapi
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home pulse
USER pulse

# There is no OS keyring in the container: pass profile secrets as environment variables,
# and publish the port on loopback only: docker run -p 127.0.0.1:8000:8000 --env-file .env …
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "pulse", "serve", "--host", "0.0.0.0", "--port", "8000", "--allow-remote"]
