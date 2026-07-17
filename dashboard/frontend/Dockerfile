# ── Stage 1: build the React frontend ─────────────────────────────
FROM node:20-slim AS fe
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ── Stage 2: combined runtime (API + static UI in one container) ──
FROM python:3.11-slim
# node is needed for server.js (static file server + /api proxy)
RUN apt-get update && apt-get install -y --no-install-recommends nodejs curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Backend deps + code
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/

# Frontend build output + proxy server
COPY --from=fe /fe/dist ./frontend/dist
COPY frontend/server.js ./frontend/server.js

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/data/dashboard.db
ENV HERMES_STATE_ROOT=/data
ENV PORT=4173
EXPOSE 4173

# Run the FastAPI backend (API on :8000) and the static/proxy server
# (UI on :4173) in the same container. server.js proxies /api/* to
# localhost:8000, so the dashboard URL serves both UI and API.
CMD ["sh", "-c", "cd /app/backend && uvicorn main:app --host 0.0.0.0 --port 8000 & node /app/frontend/server.js"]
