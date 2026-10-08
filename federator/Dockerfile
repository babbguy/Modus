# Modus Federator — Production Dockerfile
# Minimal image, single process, < 100MB.

FROM python:3.12-slim AS base

# Security: non-root user
RUN groupadd -r federator && useradd -r -g federator -d /app federator

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# Copy application
COPY . .

# Create data directory for SQLite
RUN mkdir -p /data && chown federator:federator /data

# Switch to non-root
USER federator

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8443/health')"

EXPOSE 8443

# Run with uvicorn — single worker, production settings
CMD ["uvicorn", "federator.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8443", \
     "--workers", "1", \
     "--access-log", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
