FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create runtime directories (will be overridden by Railway volumes in prod)
RUN mkdir -p data logs reports

# Non-root user for security
RUN useradd -m -u 1001 botuser && chown -R botuser:botuser /app
USER botuser

# Health check: verify the log file was written to in the last 5 minutes
# (Railway worker services don't require an HTTP health check)
HEALTHCHECK --interval=120s --timeout=15s --start-period=60s --retries=3 \
    CMD python -c "\
import os, time, sys; \
log = 'logs/bot_activity.log'; \
sys.exit(0) if os.path.exists(log) and (time.time() - os.path.getmtime(log)) < 300 else sys.exit(1)"

# Default: live mode. Override with BOT_ARGS=--paper for paper trading.
CMD python main.py ${BOT_ARGS:-}
