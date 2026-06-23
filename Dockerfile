FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs reports

# Default to paper mode — must set LIVE_TRADING_ENABLED=true in env to go live
CMD ["sh", "-c", "python main.py ${BOT_ARGS:---paper}"]
