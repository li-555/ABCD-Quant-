# =============================================================================
# ABCD-Quant — Roostoo live trading bot (v1)
# 7x24 container image.  Spot 1x LONG only; paper by default, LIVE=1 to trade.
# =============================================================================
FROM python:3.11-slim

# --- runtime environment -----------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- system deps (CA certs for HTTPS, tzdata for UTC, curl for HEALTHCHECK) ---
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/UTC /etc/localtime

# --- non-root user -----------------------------------------------------------
RUN useradd --create-home --uid 10001 --shell /bin/sh botuser

WORKDIR /app

# --- install dependencies first (better layer caching) -----------------------
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- copy application code ---------------------------------------------------
COPY bot/ ./bot/
COPY research/ ./research/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY docs/ ./docs/
COPY .env.example ./.env.example
COPY pytest.ini ./
COPY README.md ./

# Keep the logs / state directories (contents are git-ignored, recreated at run).
RUN mkdir -p bot/logs cache \
    && touch bot/logs/.gitkeep \
    && chown -R botuser:botuser /app

USER botuser

# --- entrypoint --------------------------------------------------------------
# Paper mode by default.  Set LIVE=1 (and real credentials) to place real orders.
CMD ["python", "-m", "bot.main"]

# --- health check ------------------------------------------------------------
# The scheduler writes bot/logs/heartbeat.json at the end of every cycle
# (loop is aligned to 30m bar closes + 20s, so a healthy cycle runs ~every
# 30 minutes).  Fail if the heartbeat is older than 45 minutes.
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m --retries=2 \
    CMD sh -c 'test -f bot/logs/heartbeat.json && \
        test $(( $(date +%s) - $(date -r bot/logs/heartbeat.json +%s) )) -lt 2700 || exit 1'
