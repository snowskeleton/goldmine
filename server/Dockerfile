# Build from the repository root so the addon (which clients install from here) is included:
#   docker build -f server/Dockerfile -t goldmine-server .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GOLDMINE_DATA=/data \
    GOLDMINE_PORT=8080

WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# The addon clients install and update from (Data.lua is per machine and excluded below).
COPY addon/Goldmine addon/Goldmine
COPY server/goldmine_server server/goldmine_server

RUN useradd --system --uid 1000 --home /data goldmine && mkdir -p /data && chown goldmine /data
USER goldmine
VOLUME /data
EXPOSE 8080
WORKDIR /app/server

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status == 200 else 1)"

CMD ["python", "-m", "goldmine_server.app"]
