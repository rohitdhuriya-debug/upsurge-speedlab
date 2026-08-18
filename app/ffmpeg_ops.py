"""Filter construction, command building, execution with progress parsing, loudness matching.

Everything here shells out to the ffmpeg binary. No Python wrapper libraries.
"""
import json
import math
import os
import re
import subprocess

from . import bins, db

# Set SPEEDLAB_FORCE_ATEMPO=1 to pretend rubberband is missing. Used by the self-test
# to exercise the fallback path on a machine that has rubberband.
FORCE_ATEMPO = os.environ.get("SPEEDLAB_FORCE_ATEMPO", "") not in ("", "0", "false")

# A single atempo link above 1.5 degrades badly, so chains are built from equal roots.
ATEMPO_MAX = 1.5
ATEMPO_MIN = 0.5

_caps_cache = None

TIME_RE = re.compile(r"time=(\d+):(\d{2}):(\d{2}\.?\d*)")


def fmt_num(x):
    """Trim trailing zeros: 2.0 -> '2', 1.25 -> '1.25'. Keeps filter strings readable."""
    s = ("%.6f" % float(x)).rstrip("0").rstrip(".")
    return s if s else "0"


# --------------------------------------------------------------------------- caps

def detect_capabilities(refresh=False):
    global _caps_cache
    if _caps_cache is not None and not refresh:
        return _caps_cache

    if refresh:
        bins.reload()

    rubberband = bins.RUBBERBAND
    version = "unknown"
    ffmpeg_ok = False
    try:
        ver = subprocess.run([bins.FFMPEG, "-version"], capture_output=True,
                             text=True, timeout=30)
        ffmpeg_ok = ver.returncode == 0
        first = ver.stdout.splitlines()[0] if ver.stdout else ""
        m = re.search(r"ffmpeg version (\S+)", first)
        version = m.group(1) if m else first[:80]
    except (OSError, subprocess.SubprocessError, IndexError):
        ffmpeg_ok = False

    if FORCE_ATEMPO:
        rubberband = False

    _caps_cache = {
        "rubberband": rubberband,
        "ffmpeg_version": version,
        "ffmpeg_available": ffmpeg_ok,
        "forced_atempo": FORCE_ATEMPO,
        "engine": "rubberband" if rubberband else "atempo",
        "ffmpeg_path": bins.FFMPEG,
        "ffprobe_path": bins.FFPROBE,
        "binary_source": bins.SOURCE,
        "scanned": bins.SCANNED,
    }
    return _caps_cache


def default_engine():
    return detect_capabilities()["engine"]


# ------------------------------------------------------------------------ filters

def atempo_chain(speed):
    """Decompose a tempo factor into equal roots, each link within atempo's safe range.

    1.25 -> atempo=1.25         1.75 -> atempo=1.3229,atempo=1.3229
    1.5  -> atempo=1.5          2.0  -> atempo=1.4142,atempo=1.4142
    """
    speed = float(speed)
    if speed <= 0:
        raise ValueError("speed must be positive")
    if abs(speed - 1.0) < 1e-9:
        return "atempo=1"

    limit = ATEMPO_MAX if speed > 1.0 else ATEMPO_MIN
    # 1e-9 slack so speed == limit lands on exactly one link, not two.
    links = int(math.ceil(math.log(speed) / math.log(limit) - 1e-9))
    links = max(1, links)
    root = speed ** (1.0 / links)
    return ",".join("atempo=%s" % fmt_num(round(root, 4)) for _ in range(links))


def build_audio_filter(engine, speed, audio_profile):
    """rubberband when available; atempo chain otherwise.

    transients=mixed on the default profile is a deliberate compromise for a single
    mixed track: voice wants smooth phase handling, percussive music wants crisp
    transients, and you cannot have both. Do not "optimise" this away.
    """
    s = fmt_num(speed)
    if engine == "rubberband":
        if audio_profile == "voice_only":
            return ("rubberband=tempo=%s:pitch=1:formant=preserved:"
                    "transients=smooth:detector=soft" % s)
        return ("rubberband=tempo=%s:pitch=1:formant=preserved:"
                "transients=mixed:detector=compound" % s)
    return atempo_chain(speed)


def build_video_filter(speed, target_fps):
    return "setpts=PTS/%s,fps=%d" % (fmt_num(speed), int(target_fps))


def compute_target_fps(speed, source_fps):
    """Whole-number speeds keep source cadence; fractional speeds go to 60 to avoid judder."""
    speed = float(speed)
    if abs(speed - round(speed)) < 1e-6:
        return max(1, int(round(source_fps or 30)))
    return 60


def build_command(src, out, speed, target_fps, audio_filter, has_audio):
    vf = build_video_filter(speed, target_fps)
    if has_audio:
        fc = "[0:v]%s[v];[0:a]%s[a]" % (vf, audio_filter)
        maps = ["-map", "[v]", "-map", "[a]"]
        audio_out = ["-c:a", "aac", "-b:a", "192k"]
    else:
        fc = "[0:v]%s[v]" % vf
        maps = ["-map", "[v]"]
        audio_out = ["-an"]

    return ([bins.FFMPEG, "-y", "-i", src, "-filter_complex", fc] + maps +
            ["-c:v", "libx264", "-crf", "17", "-preset", "medium",
             "-pix_fmt", "yuv420p", "-fps_mode", "cfr"] + audio_out +
            ["-movflags", "+faststart", out])


# ---------------------------------------------------------------------- execution

def _log_path(job_id):
    return os.path.join(db.LOGS, "%s.log" % job_id)


def append_log(job_id, text):
    with open(_log_path(job_id), "a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def run_with_progress(cmd, job_id, expected_duration, on_progress=None, label="encode"):
    """Run ffmpeg, stream stderr into the job log, drive progress off `time=`.

    Returns (returncode, stderr_text).
    """
    append_log(job_id, "\n=== %s ===\n$ %s\n" % (label, " ".join(_quote(c) for c in cmd)))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, bufsize=1, universal_newlines=True,
                            errors="replace")
    captured = []
    try:
        for line in proc.stderr:
            captured.append(line)
            if on_progress and expected_duration and expected_duration > 0:
                m = TIME_RE.search(line)
                if m:
                    h, mnt, sec = m.groups()
                    secs = int(h) * 3600 + int(mnt) * 60 + float(sec)
                    on_progress(max(0.0, min(0.999, secs / expected_duration)))
    finally:
        proc.stderr.close()
        proc.wait()

    stderr_text = "".join(captured)
    append_log(job_id, stderr_text)
    append_log(job_id, "--- exit code: %d ---" % proc.returncode)
    return proc.returncode, stderr_text


def _quote(part):
    return '"%s"' % part if " " in part or ";" in part else part


# ----------------------------------------------------------------------- loudness

def measure_lufs(path, job_id=None):
    """Integrated LUFS via loudnorm's analysis pass. Returns None if unmeasurable."""
    cmd = [bins.FFMPEG, "-hide_banner", "-i", path, "-af",
           "loudnorm=print_format=json", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if job_id:
        append_log(job_id, "\n=== loudness measure: %s ===\n$ %s\n%s"
                   % (os.path.basename(path), " ".join(_quote(c) for c in cmd),
                      proc.stderr[-4000:]))
    # The JSON block is the last {...} in stderr.
    start = proc.stderr.rfind("{")
    end = proc.stderr.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(proc.stderr[start:end + 1])
    except json.JSONDecodeError:
        return None
    try:
        value = float(data.get("input_i"))
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def apply_gain(src, out, gain_db, job_id):
    """Re-encode audio only, with a fixed gain. Video is copied untouched."""
    cmd = [bins.FFMPEG, "-y", "-i", src, "-c:v", "copy",
           "-af", "volume=%sdB" % fmt_num(round(gain_db, 2)),
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    code, _ = run_with_progress(cmd, job_id, None, None, label="loudness remux")
    return code == 0 and os.path.exists(out)
