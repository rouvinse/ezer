# ── EZER / ACIA — Production Dockerfile ──────────────────────────────────────
# Multi-stage build: builder installs deps, final image is lean.
#
# Build:   docker build -t ezer .
# Run:     docker run -e DATABASE_URL=... -e ACIA_ADMIN_KEY=... -p 8000:8000 ezer
# Railway: Railway uses NIXPACKS by default (railway.toml). Switch to this
#          Dockerfile by setting [build] builder = "DOCKERFILE" in railway.toml.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Final image ───────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN useradd -r -u 1001 -m acia
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=acia:acia . .

# Remove dev/test artifacts from the image
RUN rm -rf tests/ ezer_tests/ source_code/ docs/ dashboard/ \
           __pycache__ */__pycache__ */*/__pycache__ \
           .env* *.db *.sqlite *.pkl *.joblib

USER acia

# Railway injects PORT at runtime
ENV PORT=8000
EXPOSE $PORT

# Health check — Railway and load balancers poll /health
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')" || exit 1

CMD ["python", "server.py"]
