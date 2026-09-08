FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY templates templates
COPY static static

# The compose file bind-mounts these four; the defaults only matter for a bare
# docker run.
ENV FLEETDECK_CONFIG=/config/fleetdeck.yml \
    FLEETDECK_CATALOG=/config/catalog.yml \
    FLEETDECK_KNOWN_HOSTS=/config/known_hosts \
    FLEETDECK_DB=/data/fleetdeck.db \
    FLEETDECK_RUNS=/data/runs \
    FLEETDECK_SECRETS=/secrets \
    FLEETDECK_SSH_IDENTITY=/keys/fleetdeck_ed25519 \
    FLEETDECK_PORT=8310
VOLUME /data
EXPOSE 8310
USER app

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD curl -fs http://127.0.0.1:8310/api/health || exit 1

CMD ["python", "-m", "app"]
