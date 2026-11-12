FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GARMIN_HOSTED=true \
    GARMIN_WEB_PORT=8000 \
    AI_PROVIDER=api \
    GARMIN_TOKENSTORE=data/garmin_tokens

WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY garmin_ai garmin_ai
COPY web/dist web/dist
COPY cli.py .

# Everything that must survive restarts (database, Garmin token, settings) lives in /app/data.
RUN useradd --create-home --uid 10001 app && mkdir data && chown app:app data
USER app
VOLUME /app/data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "-m", "garmin_ai.web_server"]
