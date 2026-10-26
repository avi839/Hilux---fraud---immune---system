FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN node node_modules/vite/bin/vite.js build

FROM python:3.10-slim
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt  
COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist
ENV PYTHONPATH=/app
CMD python -m helix.api --config backend/config/demo.json --generator --soc --attacker --immune --static-dir /app/frontend/dist
