# ── Stage 1: build the React/vite frontend → dashboard/frontend/dist ──────────
# The Python backend serves this dist/ at / (no nginx). Built here so Railway
# needs no committed build artifacts and the repo stays lean. [GUARD L62]
FROM node:20-slim AS frontend
WORKDIR /fe
COPY dashboard/frontend/package.json dashboard/frontend/package-lock.json ./
RUN npm ci
COPY dashboard/frontend/ ./
RUN npm run build   # → /fe/dist

# ── Stage 2: Python runtime (bots + dashboard share one image) ────────────────
FROM python:3.11-slim

WORKDIR /app

# Install uv for reproducible dependency installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY hermes_core/ hermes_core/
COPY bots/ bots/
COPY dashboard/ dashboard/

# Drop in the built frontend from stage 1 at the path the backend expects
# (dashboard/backend/main.py → ../frontend/dist).
COPY --from=frontend /fe/dist dashboard/frontend/dist

RUN uv sync --frozen --no-dev

ENV HERMES_BOT_NAME=forex
ENV PYTHONUNBUFFERED=1

# Code is read-only; mount /data for persistent state (D2). The container's
# start command is set per-service in railway.json (HERMES_BOT_NAME selects
# which bot, or the dashboard entrypoint for the web service).
ENTRYPOINT ["uv", "run", "python"]
CMD ["bots/forex/main.py"]
