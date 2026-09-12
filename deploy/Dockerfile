FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADING_LIVE_MONEY_ACTIVE=false \
    PRAMANA_PAPER_DB=/data/pramana.db \
    PRAMANA_XAI_DIR=/data/xai \
    PRAMANA_GHOST_LOG=/data/pramana-ghost.log

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . kiteconnect ib_async \
    && useradd --create-home --uid 10001 pramana \
    && mkdir -p /data/xai \
    && chown -R pramana:pramana /app /data

USER pramana
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "quant_ai.daemon"]
