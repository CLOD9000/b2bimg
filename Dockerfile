# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — builder: installa le dipendenze in un virtualenv isolato
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential serve solo se non sono disponibili wheel precompilate
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime: immagine minimale, utente non-root, healthcheck
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    DATA_DIR=/data \
    LOG_LEVEL=INFO

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY plugins ./plugins
COPY templates ./templates
COPY static ./static
COPY pyproject.toml README.md ./

RUN mkdir -p /data/uploads /data/output \
    && chown -R app:app /data /app

USER app
EXPOSE 8000

# Il container ha solo Python: l'healthcheck usa urllib (nessun curl necessario).
# Shell form per espandere ${APP_PORT}.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; p=os.environ.get('APP_PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=4).status == 200 else 1)"

# Il bind interno è SEMPRE 0.0.0.0 (l'IP LAN è della macchina host, non del
# container): l'isolamento si ottiene con la mappatura "ports:" del compose.
# La porta ascoltata segue APP_PORT. Shell form necessaria per espandere la env.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${APP_PORT:-8000}\" --no-server-header --proxy-headers"]
