# Modus — Simple Docker image (single-stage, includes aiosqlite)
# For quick deploys and SQLite-backed setups.
# Production multi-stage build: use Dockerfile.orchestrator instead.
FROM python:3.12-slim

WORKDIR /app

# Install system deps for psycopg2 and uvloop
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir aiosqlite

# Copy application code
COPY orchestrator/ orchestrator/
COPY migrations/ migrations/
COPY alembic.ini .
COPY dashboard/ dashboard/

# Plugins directory (custom policy extensions)
RUN mkdir -p plugins
COPY plugins/ plugins/

# The version the service reports, baked in at build time: publish.yml
# passes the release version (docker build --build-arg MODUS_BUILD_VERSION=1.2.0).
# Without it, the image reports the version in the source. The image tag
# (MODUS_VERSION in deploy/.env) does not change it.
ARG MODUS_BUILD_VERSION=""
LABEL org.opencontainers.image.version="${MODUS_BUILD_VERSION}"
RUN if [ -n "$MODUS_BUILD_VERSION" ]; then \
        echo "$MODUS_BUILD_VERSION" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.+-]+)?' \
            || { echo "MODUS_BUILD_VERSION must be a version such as 1.2.0, not '$MODUS_BUILD_VERSION'" >&2; exit 1; }; \
        printf '%s\n' "$MODUS_BUILD_VERSION" > orchestrator/BUILD_VERSION; \
    fi

# Migration-aware entrypoint (runs alembic for Postgres, skips for SQLite)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root user
RUN groupadd -r modus && useradd -r -g modus -d /app -s /sbin/nologin modus
RUN mkdir -p /app/data && chown -R modus:modus /app
USER modus

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--loop", "auto", "--no-access-log"]
