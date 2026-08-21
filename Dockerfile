# UPSURGE SpeedLab - reproducible image with a rubberband-capable ffmpeg.
FROM python:3.12-slim

# Debian's ffmpeg is built --enable-librubberband, which is the whole point of
# containerising this: the preferred audio engine without a source build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# All mutable state (db, inbox, work, outbox, logs, manifests) lives here so it
# can be bind-mounted and survive container rebuilds.
ENV SPEEDLAB_ROOT=/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5070
# Hosts inject their own $PORT; fall back to 5070 for local/compose use.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-5070}"]
