"""ffprobe wrapper: media inspection, fps parsing, reel/longform classification, thumbnails."""
import hashlib
import json
import os
import subprocess

from . import bins, db

REEL_MAX_SECONDS = 90.0


class ProbeError(Exception):
    pass


def parse_rate(value):
    """r_frame_rate arrives as a fraction string like '30000/1001'."""
    if not value:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den = float(den)
            if den == 0:
                return 0.0
            return float(num) / den
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _float(value, default=0.0):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def raw_probe(path):
    cmd = [bins.FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProbeError("ffprobe failed on %s: %s" % (os.path.basename(path),
                                                       proc.stderr.strip()[:400]))
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned unparseable JSON: %s" % exc)


def probe(path):
    """Normalised media facts used by every later stage."""
    data = raw_probe(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if v is None:
        raise ProbeError("no video stream found")

    fps = parse_rate(v.get("r_frame_rate"))
    if fps <= 0:
        fps = parse_rate(v.get("avg_frame_rate"))
    if fps <= 0:
        fps = 30.0

    duration = _float(fmt.get("duration"))
    if duration <= 0:
        duration = _float(v.get("duration"))

    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)

    return {
        "fps": round(fps, 6),
        "duration": round(duration, 4),
        "width": width,
        "height": height,
        "vcodec": v.get("codec_name"),
        "acodec": a.get("codec_name") if a else None,
        "has_audio": a is not None,
        "video_duration": round(_float(v.get("duration"), duration), 4),
        "audio_duration": round(_float(a.get("duration"), duration), 4) if a else None,
        "nb_frames": int(v.get("nb_frames")) if str(v.get("nb_frames", "")).isdigit() else None,
        "size_bytes": int(_float(fmt.get("size"))),
        "format_name": fmt.get("format_name"),
    }


def classify(info):
    """Vertical and <= 90s is a reel; everything else is longform."""
    if info["height"] > info["width"] and 0 < info["duration"] <= REEL_MAX_SECONDS:
        return "reel"
    return "longform"


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def make_thumbnail(src_path, job_id, duration):
    """Single frame at 10% of duration, 160px wide."""
    out = os.path.join(db.THUMBS, "%s.jpg" % job_id)
    seek = max(0.0, duration * 0.10) if duration > 0 else 0.0
    cmd = [bins.FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % seek, "-i", src_path,
           "-frames:v", "1", "-vf", "scale=160:-2", "-q:v", "4", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out):
        # Retry from the first frame; short or oddly-indexed files can fail the seek.
        cmd = [bins.FFMPEG, "-y", "-v", "error", "-i", src_path, "-frames:v", "1",
               "-vf", "scale=160:-2", "-q:v", "4", out]
        subprocess.run(cmd, capture_output=True, text=True)
    return out if os.path.exists(out) else None
