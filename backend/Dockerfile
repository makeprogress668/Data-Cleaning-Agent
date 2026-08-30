# syntax=docker/dockerfile:1

# ── Stage 1: build a wheel so the runtime image carries no build toolchain ──────
FROM python:3.11-slim AS builder

WORKDIR /build

# Copy only what the build needs first, so dependency resolution is cached.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip build \
    && python -m build --wheel --outdir /dist

# ── Stage 2: slim runtime image ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# uvicorn needs no compiler at runtime; keep the image small and CVE-light.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_AGENT_API_WORK_DIR=/data/api_jobs

WORKDIR /app

# Install API + production persistence extras. Local source installs can keep using
# the file backend without PostgreSQL dependencies.
COPY --from=builder /dist/*.whl /tmp/
RUN python -m pip install --upgrade pip \
    && python -m pip install "$(ls /tmp/*.whl)[api,production]" \
    && rm -rf /tmp/*.whl

# Install the entrypoint before dropping privileges.
COPY docker/backend-entrypoint.sh /usr/local/bin/backend-entrypoint.sh
RUN chmod +x /usr/local/bin/backend-entrypoint.sh

# Run as an unprivileged user with a writable job directory.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/api_jobs \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

# Production uses PostgreSQL + ARQ + object storage, so API workers only accept and
# dispatch requests. Local/file mode still runs one uvicorn process with a thread pool.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else sys.exit(1)"

ENTRYPOINT ["backend-entrypoint.sh"]
