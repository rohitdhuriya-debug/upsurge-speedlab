"""The five QA checks. Nothing reaches outbox without passing all of them."""
import os
import subprocess

from . import bins
from .probe import ProbeError, parse_rate, raw_probe, _float

DURATION_TOLERANCE = 0.15   # seconds, expected vs actual output duration
AV_DRIFT_TOLERANCE = 0.10   # seconds, video stream vs audio stream
FPS_TOLERANCE = 0.01
MIN_SIZE_BYTES = 10 * 1024


def _count_frames(path, stream):
    """nb_frames when the container carries it, else count packets, else estimate."""
    raw = stream.get("nb_frames")
    if str(raw).isdigit() and int(raw) > 0:
        return int(raw), "nb_frames"

    cmd = [bins.FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
           "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    value = proc.stdout.strip().split(",")[0] if proc.stdout else ""
    if value.isdigit() and int(value) > 0:
        return int(value), "nb_read_packets"

    est = _float(stream.get("duration")) * parse_rate(stream.get("r_frame_rate"))
    return int(est), "estimated"


def run_qa(out_path, src_duration, speed, target_fps, src_has_audio):
    """Returns a dict with per-check results and an overall `passed` flag."""
    checks = []

    def add(name, passed, detail, **extra):
        row = {"name": name, "passed": bool(passed), "detail": detail}
        row.update(extra)
        checks.append(row)
        return passed

    # 1. exists and parses
    if not out_path or not os.path.exists(out_path):
        add("output_exists", False, "output file missing")
        return {"passed": False, "checks": checks}

    size = os.path.getsize(out_path)
    try:
        data = raw_probe(out_path)
    except ProbeError as exc:
        add("output_exists", False, "ffprobe could not parse output: %s" % exc)
        return {"passed": False, "checks": checks}

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if v is None:
        add("output_exists", False, "output has no video stream")
        return {"passed": False, "checks": checks}

    add("output_exists", True, "file exists and ffprobe parses it")

    # 2. duration matches src/speed
    actual = _float(fmt.get("duration"))
    expected = (src_duration / speed) if speed else 0.0
    delta = abs(actual - expected)
    add("duration_match", delta < DURATION_TOLERANCE,
        "actual %.3fs vs expected %.3fs (delta %.3fs, tolerance %.2fs)"
        % (actual, expected, delta, DURATION_TOLERANCE),
        actual=round(actual, 3), expected=round(expected, 3), delta=round(delta, 3))

    # 3. A/V drift
    if a is not None:
        vd = _float(v.get("duration"), actual)
        ad = _float(a.get("duration"), actual)
        drift = abs(vd - ad)
        add("av_drift", drift < AV_DRIFT_TOLERANCE,
            "video %.3fs vs audio %.3fs (drift %.3fs, tolerance %.2fs)"
            % (vd, ad, drift, AV_DRIFT_TOLERANCE), drift=round(drift, 3))
    else:
        add("av_drift", not src_has_audio,
            "no audio stream in output"
            + ("" if not src_has_audio else " but source had one"))

    # 4. frames and size
    frames, source = _count_frames(out_path, v)
    add("frames_and_size", frames > 0 and size > MIN_SIZE_BYTES,
        "%d frames (%s), %d bytes (minimum %d)" % (frames, source, size, MIN_SIZE_BYTES),
        frames=frames, size_bytes=size)

    # 5. fps matches target
    out_fps = parse_rate(v.get("r_frame_rate"))
    fps_delta = abs(out_fps - float(target_fps))
    add("fps_match", fps_delta <= FPS_TOLERANCE,
        "output %.4f fps vs target %d fps (delta %.4f, tolerance %.2f)"
        % (out_fps, int(target_fps), fps_delta, FPS_TOLERANCE),
        out_fps=round(out_fps, 4), target_fps=int(target_fps))

    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "out_duration": round(actual, 3),
        "out_fps": round(out_fps, 4),
        "size_bytes": size,
    }
