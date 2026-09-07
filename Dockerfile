# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# Metadata (values filled by the build, see Makefile)
ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="hassgram" \
      org.opencontainers.image.description="Telegram bot for Home Assistant: lights, temperatures and voice commands, in Italian and English." \
      org.opencontainers.image.source="https://github.com/lordraw77/hassgram" \
      org.opencontainers.image.url="https://hub.docker.com/r/lordraw/hassgram" \
      org.opencontainers.image.documentation="https://github.com/lordraw77/hassgram/blob/main/README.md" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py entities.py ha_client.py i18n.py ./

# The bot only makes outbound connections: no port to expose, no state to keep.
RUN useradd --create-home --uid 10001 hassgram
USER hassgram

CMD ["python3", "bot.py"]
