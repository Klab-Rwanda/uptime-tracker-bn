# ---- base: runtime dependencies shared by every image ------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=120 --retries 10 -r requirements.txt

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- dev: adds test/lint tools (used by docker-compose) ----------------------
FROM base AS dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir --default-timeout=120 --retries 10 -r requirements-dev.txt

COPY . .

# ---- prod: runtime only (default target for `docker build`) ------------------
FROM base AS prod

COPY . .
