FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./frontend/
COPY docs/ ./docs/
COPY frontend/ ./frontend/
WORKDIR /build/frontend
RUN npm ci
RUN node node_modules/vite/bin/vite.js build

FROM python:3.10-slim
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ ./backend/
COPY --from=frontend /build/frontend/dist ./frontend/dist
ENV PYTHONPATH=/app/backend
CMD python -m helix.api --config backend/config/demo.json --generator --soc --attacker --immune