# ==============================================================================
# Stage 1: Builder
# ==============================================================================
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cargo \
    rustc \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ==============================================================================
# Stage 2: Runner
# ==============================================================================
FROM python:3.12-slim-bookworm AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TELEVAULT_BIND_HOST=0.0.0.0 \
    TELEVAULT_PORT=8000 \
    TELEVAULT_HEADLESS=1

# Create non-root user madhu (UID 1000, GID 1000)
RUN groupadd -g 1000 madhu && \
    useradd -u 1000 -g madhu -m -s /bin/bash madhu

WORKDIR /app

# Pre-create stateful directories owned by madhu:madhu
RUN mkdir -p /app/session /app/data /app/out && \
    chown -R madhu:madhu /app

# Copy virtual environment from builder
COPY --from=builder --chown=madhu:madhu /opt/venv /opt/venv

# Copy application source code
COPY --chown=madhu:madhu . /app

USER madhu

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

ENTRYPOINT ["python", "run_web.py"]
