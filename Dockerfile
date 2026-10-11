# Production Dockerfile for 24/7 Flipkart Restock Monitor
FROM python:3.12-slim

WORKDIR /app

# Install dependencies into standard system site-packages (accessible by all users)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ /app/app/

# Create persistent directories and configure non-root user (UID 1000 for Linux host bind mounts)
RUN (groupadd -g 1000 appgroup 2>/dev/null || groupadd -r appgroup) \
    && (useradd -u 1000 -g appgroup -d /app -s /bin/sh appuser 2>/dev/null || useradd -r -g appgroup -d /app -s /bin/sh appuser) \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appgroup /app

# Run as non-root user for security
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

# Internal healthcheck port
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).getcode() == 200 else 1)"

ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["run"]
