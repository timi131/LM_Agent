FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV LM_AGENT_WEB_HOST=0.0.0.0 \
    LM_AGENT_WEB_PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "exec .venv/bin/lm-agent-web --host 0.0.0.0 --port \"${PORT:-8000}\""]
