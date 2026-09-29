# One image for every Python service; the compose file picks the module to run.
FROM python:3.11-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/services

WORKDIR /app

COPY services/requirements.txt services/requirements.txt
RUN pip install -r services/requirements.txt

COPY alembic.ini ./
COPY migrations migrations
COPY services services

RUN useradd --uid 10001 --create-home uft \
    && mkdir -p /data/bank /data/transcripts \
    && chown -R uft /data
USER uft

EXPOSE 8000

# Experiment tooling: the app image plus the docker CLI and the lab package.
FROM app AS lab
USER root
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY lab lab
ENV PYTHONPATH=/app/services:/app
