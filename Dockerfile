FROM python:3.12-slim

# FFmpeg is needed for audio/video merging, format conversion and splitting oversized
# files; aria2 gives segmented, resumable downloads. Both are optional at runtime —
# `grabber doctor` reports what is present — but a full-featured image ships them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg aria2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY media_bot ./media_bot

RUN pip install --no-cache-dir .

# The bot writes downloads/cookies/state here; mount it so jobs survive restarts.
ENV DATA_DIR=/data
RUN mkdir -p /data

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=20s --start-period=30s \
    CMD python -c "import sys; from media_bot.config import load_dotenv, load_settings; \
load_dotenv(); load_settings(); sys.exit(0)"

CMD ["grabber", "run"]
