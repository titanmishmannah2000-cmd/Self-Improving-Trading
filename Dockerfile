FROM python:3.11-slim

WORKDIR /app

# Install uv for reproducible dependency installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY hermes_core/ hermes_core/
COPY bots/ bots/
COPY dashboard/ dashboard/

RUN uv sync --frozen --no-dev

ENV HERMES_BOT_NAME=forex
ENV PYTHONUNBUFFERED=1

# Code is read-only; mount /data for persistent state (D2). The container's
# start command is set per-service in railway.json (HERMES_BOT_NAME selects
# which bot, or the dashboard entrypoint for the web service).
ENTRYPOINT ["uv", "run", "python"]
CMD ["bots/forex/main.py"]
