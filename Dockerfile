# syntax=docker/dockerfile:1
#
# Spotifast + Sunnify (unmodified) in one image.
#
# Debian trixie on purpose: the PyQt6 wheels Sunnify depends on need glibc
# 2.34 (x86_64) / 2.39 (arm64); bookworm only has 2.36.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    QT_QPA_PLATFORM=offscreen \
    HOME=/tmp \
    TZ=Europe/Paris

# ffmpeg for Sunnify's transcoding; the libraries are exactly what Qt's
# QtCore/QtGui/QtWidgets link against (checked with readelf on the wheels).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg tini ca-certificates tzdata \
      libglib2.0-0t64 libegl1 libgl1 libx11-6 libdbus-1-3 libfontconfig1 \
      libfreetype6 libxkbcommon0 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pinned, hash-checked dependencies (regenerate with scripts/lock.sh).
COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock

# yt-dlp is the one dependency that must stay fresh: YouTube changes often
# and old releases stop working. Bump YTDLP_REFRESH (or build with
# --no-cache) to pull the latest release.
ARG YTDLP_REFRESH=1
RUN pip install --upgrade --no-deps yt-dlp yt-dlp-ejs

# Sunnify, installed exactly as published (its licence forbids changes).
COPY vendor/sunnify /tmp/sunnify
RUN pip install --no-deps /tmp/sunnify && rm -rf /tmp/sunnify

COPY spotifast ./spotifast

RUN useradd --system --uid 10001 --user-group --home-dir /nonexistent \
      --shell /usr/sbin/nologin spotifast \
 && mkdir -p /data /work \
 && chown spotifast:spotifast /data /work

# Fail the build now rather than at the first download.
RUN sunnify --version \
 && python -c "import PyQt6.QtWidgets, Spotify_Downloader, spotifast.main" \
 && deno --version \
 && ffmpeg -hide_banner -version | head -n1

USER spotifast
ENV SPOTIFAST_DATA_DIR=/data \
    SPOTIFAST_WORK_DIR=/work \
    SPOTIFAST_SUNNIFY_BIN=sunnify

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

ENTRYPOINT ["tini", "--"]
# --proxy-headers / --forwarded-allow-ips '*': the app is only reachable
# through Caddy on the private compose network (no published port), and
# Caddy overwrites X-Forwarded-For with the real client address.
CMD ["uvicorn", "--factory", "spotifast.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header", "--timeout-graceful-shutdown", "20"]
