FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY website/frontend/package.json website/frontend/package-lock.json ./
RUN npm ci
COPY website/frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY website/backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt
COPY website/backend/ /app/backend/
COPY --from=frontend-build /build/dist /app/frontend/dist
WORKDIR /app/backend
RUN python scripts/download_models.py \
    && python -c "from app.face.engine import engine; engine.load()"
EXPOSE 8000
CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
