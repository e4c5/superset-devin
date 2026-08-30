FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY simulator ./simulator
COPY scripts ./scripts

RUN mkdir -p /srv/data && useradd --create-home --uid 10001 orchestrator \
    && chown -R orchestrator:orchestrator /srv
USER orchestrator

ENV DATABASE_PATH=/srv/data/state.db \
    REPORT_PATH=/srv/data/report.md \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8080\")}/healthz').read()"

CMD ["sh", "-c", "uvicorn app.webhook:app --host 0.0.0.0 --port ${PORT:-8080}"]
