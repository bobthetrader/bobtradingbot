FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI for the AI Trade Desk (core/trade_agent.py) — authenticates
# with CLAUDE_CODE_OAUTH_TOKEN (Max subscription, no metered API billing).
# Bot degrades gracefully to rules-only if this layer is missing or unauthed.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs reports

# Default to paper mode — must set LIVE_TRADING_ENABLED=true in env to go live
CMD ["sh", "-c", "python main.py ${BOT_ARGS:---paper}"]
