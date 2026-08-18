"""End-to-end self-test. Boots the real server on a scratch root and drives the HTTP API.

Run:  .venv/bin/python scripts/selftest.py     (or  .venv\\Scripts\\python scripts\\selftest.py)
"""
import http.cookiejar
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(ROOT, "selftest_run")
FIXTURES = os.path.join(SCRATCH, "fixtures")
PORT = int(os.environ.get("SPEEDLAB_TEST_PORT", "5071"))
BASE = "http://127.0.0.1:%d" % PORT
FFMPEG = os.environ.get("SPEEDLAB_FFMPEG", "ffmpeg")

SINE_HZ = 220.0
# Batches are owned by a session cookie, so the test drives the API the way a
# browser does - one jar for the whole run.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))

results = []


def record(name, passed, detail):
    results.append((name, passed, detail))
    print("  [%s] %s\n        %s" % ("PASS" if passed else "FAIL", name, detail), flush=True)


# ------------------------------------------------------------------ http helpers

def get(path):
    with _OPENER.open(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def post_json(path, payload=None, method="POST"):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def upload(paths, batch_id=None, match_loudness=False):
    boundary = "----speedlab" + uuid.uuid4().hex
    body = b""
    for key, value in (("match_loudness", "1" if match_loudness else "0"),
                       ("batch_id", batch_id or "")):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, key, value)).encode()
    for p in paths:
        with open(p, "rb") as fh:
            blob = fh.read()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
                 "Content-Type: video/mp4\r\n\r\n" % (boundary, os.path.basename(p))).encode()
        body += blob + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()

    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST",
                                 headers={"Content-Type":
                                          "multipart/form-data; boundary=%s" % boundary})
    with _OPENER.open(req, timeout=300) as r:
        return json.loads(r.read().decode())


def wait_for_batch(batch_id, timeout=900):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        state = get("/api/batches/" + batch_id)
        active = [j for j in state["jobs"] if j["status"] in ("queued", "running")]
        if not active:
            return state
        cur = [(j["status"], j["stage"], round(j["progress"] or 0, 2)) for j in state["jobs"]]
        if cur != last:
            print("        ... %s" % cur, flush=True)
            last = cur
        time.sleep(1.0)
    raise RuntimeError("batch %s did not finish within %ds" % (batch_id, timeout))


# ------------------------------------------------------------------- ffmpeg utils

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (" ".join(cmd), p.stderr[-2000:]))
    return p


def probe_out(path):
    p = run(["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path])
    data = json.loads(p.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    return {
        "duration": float(data["format"]["duration"]),
        "fps": float(num) / float(den),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


def fundamental_hz(path, start=1.0, window=0.5, lo=120, hi=700):
    """Peak frequency via Goertzel over a decoded 8 kHz mono window.

    A pure sine in, a pure sine out: if the pipeline resampled instead of
    time-stretching, this number would move by the speed factor.
    """
    sr = 8000
    p = subprocess.run([FFMPEG, "-v", "error", "-ss", str(start), "-t", str(window),
                        "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True)
    raw = p.stdout
    n = len(raw) // 2
    if n < sr * window * 0.5:
        raise RuntimeError("not enough audio decoded from %s" % path)
    samples = struct.unpack("<%dh" % n, raw[:n * 2])

    best_f, best_mag = 0.0, -1.0
    for f in range(lo, hi + 1):
        w = 2.0 * math.pi * f / sr
        coeff = 2.0 * math.cos(w)
        s1 = s2 = 0.0
        for x in samples:
            s0 = x + coeff * s1 - s2
            s2, s1 = s1, s0
        mag = s1 * s1 + s2 * s2 - coeff * s1 * s2
        if mag > best_mag:
            best_mag, best_f = mag, float(f)
    return best_f


# ---------------------------------------------------------------------- fixtures

def build_fixtures():
    os.makedirs(FIXTURES, exist_ok=True)
    clip = os.path.join(FIXTURES, "speedlab_test_10s.mp4")
    silent = os.path.join(FIXTURES, "speedlab_test_noaudio.mp4")

    run([FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=10" % SINE_HZ,
         "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-t", "10", clip])

    run([FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=10",
         "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-an", "-t", "10", silent])

    small = []
    for i in range(6):
        path = os.path.join(FIXTURES, "batch_clip_%d.mp4" % i)
        run([FFMPEG, "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=3",
             "-f", "lavfi",
             "-i", "sine=frequency=%d:sample_rate=48000:duration=3" % (200 + i * 20),
             "-c:v", "libx264", "-crf", "24", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-t", "3", path])
        small.append(path)

    quiet = os.path.join(FIXTURES, "quiet_source.mp4")
    run([FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=5",
         "-af", "volume=-14dB",
         "-c:v", "libx264", "-crf", "24", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-t", "5", quiet])

    return clip, silent, small, quiet


# ------------------------------------------------------------------------ server

def start_server():
    env = dict(os.environ)
    env["SPEEDLAB_ROOT"] = SCRATCH
    env["PYTHONPATH"] = ROOT
    os.makedirs(os.path.join(SCRATCH, "logs"), exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=open(os.path.join(SCRATCH, "server.log"), "w"),
        stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            caps = get("/api/capabilities")
            return proc, caps
        except (urllib.error.URLError, OSError):
            if proc.poll() is not None:
                raise RuntimeError("server exited early; see %s"
                                   % os.path.join(SCRATCH, "server.log"))
            time.sleep(0.5)
    raise RuntimeError("server did not come up on port %d" % PORT)


def process(paths, speed, match_loudness=False):
    """Upload, set speed on every row, process, wait. Returns the final batch state."""
    res = upload(paths, match_loudness=match_loudness)
    batch = res["batch_id"]
    for job_id in res["job_ids"]:
        job = next(j for j in get("/api/batches/" + batch)["jobs"] if j["id"] == job_id)
        if job["status"] == "queued":
            post_json("/api/jobs/" + job_id, {"speed": speed}, method="PATCH")
    post_json("/api/batches/%s/start" % batch)
    return wait_for_batch(batch)


def _named(result, check_name):
    """True when the named check passed."""
    for c in result.get("checks", []):
        if c["name"] == check_name:
            return c["passed"]
    return False


def qa_summary(job):
    qa = job.get("qa") or {}
    return ", ".join("%s=%s" % (c["name"], "ok" if c["passed"] else "FAIL")
                     for c in qa.get("checks", []))


# -------------------------------------------------------------------------- main

def main():
    # Keep every artifact (db, work, outbox, logs) inside the scratch root.
    os.environ["SPEEDLAB_ROOT"] = SCRATCH
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH)

    print("Building fixtures...", flush=True)
    clip, silent, small_clips, quiet = build_fixtures()
    src = probe_out(clip)
    print("  source: %.3fs @ %.3f fps, audio=%s" % (src["duration"], src["fps"],
                                                    src["has_audio"]), flush=True)

    print("Starting server on port %d (scratch root: %s)" % (PORT, SCRATCH), flush=True)
    proc, caps = start_server()
    print("  engine: %s (rubberband=%s, ffmpeg %s)"
          % (caps["engine"], caps["rubberband"], caps["ffmpeg_version"]), flush=True)

    try:
        # ---------------------------------------------------------------- test 1
        print("\nTEST 1: 1.5x -> ~6.67s, 60fps", flush=True)
        state = process([clip], 1.5)
        job15 = state["jobs"][0]
        out15 = job15.get("out_path")
        if job15["status"] != "done":
            record("1. 1.5x output timing", False, "job status %s: %s"
                   % (job15["status"], job15.get("error")))
        else:
            info = probe_out(out15)
            expected = src["duration"] / 1.5
            ok = abs(info["duration"] - expected) < 0.15 and abs(info["fps"] - 60) < 0.01
            record("1. 1.5x output timing", ok,
                   "duration %.3fs (expected %.3fs), fps %.3f (expected 60)"
                   % (info["duration"], expected, info["fps"]))

        # ---------------------------------------------------------------- test 2
        print("\nTEST 2: 2.0x -> ~5.0s, 30fps (source cadence)", flush=True)
        state2 = process([clip], 2.0)
        job20 = state2["jobs"][0]
        out20 = job20.get("out_path")
        if job20["status"] != "done":
            record("2. 2.0x output timing", False, "job status %s: %s"
                   % (job20["status"], job20.get("error")))
        else:
            info = probe_out(out20)
            expected = src["duration"] / 2.0
            ok = abs(info["duration"] - expected) < 0.15 and abs(info["fps"] - 30) < 0.01
            record("2. 2.0x output timing", ok,
                   "duration %.3fs (expected %.3fs), fps %.3f (expected 30)"
                   % (info["duration"], expected, info["fps"]))

        # ---------------------------------------------------------------- test 3
        print("\nTEST 3: all five QA checks pass on both", flush=True)
        both_ok = True
        details = []
        for label, job in (("1.5x", job15), ("2.0x", job20)):
            qa = job.get("qa") or {}
            checks = qa.get("checks", [])
            passed = bool(qa.get("passed")) and len(checks) == 5
            both_ok = both_ok and passed
            details.append("%s: %d checks, %s" % (label, len(checks), qa_summary(job)))
        record("3. QA gate (5 checks x 2 files)", both_ok, " | ".join(details))

        # ---------------------------------------------------------------- test 4
        print("\nTEST 4: 1.5x output fed back in is caught as a duplicate", flush=True)
        if out15 and os.path.exists(out15):
            res = upload([out15])
            dup_state = get("/api/batches/" + res["batch_id"])
            dup_job = dup_state["jobs"][0]
            ok = dup_job["status"] == "skipped_duplicate"
            record("4. duplicate protection", ok,
                   "status=%s, message=%s" % (dup_job["status"],
                                              (dup_job.get("error") or "")[:80]))
        else:
            record("4. duplicate protection", False, "no 1.5x output to re-feed")

        # ---------------------------------------------------------------- test 5
        print("\nTEST 5: file with no audio stream processes without crashing", flush=True)
        state5 = process([silent], 1.5)
        job_na = state5["jobs"][0]
        info_na = probe_out(job_na["out_path"]) if job_na.get("out_path") else None
        ok = (job_na["status"] == "done" and info_na is not None
              and not info_na["has_audio"] and abs(info_na["fps"] - 60) < 0.01)
        record("5. no-audio passthrough", ok,
               "status=%s%s" % (job_na["status"],
                                "" if info_na is None else
                                ", out %.3fs @ %.2f fps, audio=%s"
                                % (info_na["duration"], info_na["fps"], info_na["has_audio"])))

        # ---------------------------------------------------------------- test 6
        print("\nTEST 6: atempo fallback chain", flush=True)
        sys.path.insert(0, ROOT)
        os.environ["SPEEDLAB_FORCE_ATEMPO"] = "1"
        from app import ffmpeg_ops
        ffmpeg_ops.FORCE_ATEMPO = True
        ffmpeg_ops.detect_capabilities(refresh=True)

        expected_chains = {
            1.25: "atempo=1.25",
            1.5: "atempo=1.5",
            1.75: "atempo=1.3229,atempo=1.3229",
            2.0: "atempo=1.4142,atempo=1.4142",
        }
        chain_ok = True
        chain_detail = []
        for spd, want in expected_chains.items():
            got = ffmpeg_ops.build_audio_filter("atempo", spd, "voice_music")
            chain_ok = chain_ok and got == want
            chain_detail.append("%sx -> %s" % (spd, got))
            if max(float(part.split("=")[1]) for part in got.split(",")) > 1.5 + 1e-9:
                chain_ok = False

        # The engine must be atempo when rubberband is force-disabled...
        engine_ok = ffmpeg_ops.default_engine() == "atempo"
        # ...and the chain must be one ffmpeg actually accepts.
        probe_cmd = [FFMPEG, "-v", "error", "-f", "lavfi",
                     "-i", "sine=frequency=220:duration=1", "-af",
                     expected_chains[1.75], "-f", "null", "-"]
        accepted = subprocess.run(probe_cmd, capture_output=True, text=True).returncode == 0

        # And the rubberband strings must still be built to spec when it is present.
        rb_music = ffmpeg_ops.build_audio_filter("rubberband", 1.5, "voice_music")
        rb_voice = ffmpeg_ops.build_audio_filter("rubberband", 1.5, "voice_only")
        rb_ok = (rb_music == "rubberband=tempo=1.5:pitch=1:formant=preserved:"
                             "transients=mixed:detector=compound"
                 and rb_voice == "rubberband=tempo=1.5:pitch=1:formant=preserved:"
                                 "transients=smooth:detector=soft")

        record("6. atempo fallback chain", chain_ok and engine_ok and accepted and rb_ok,
               "%s | engine=%s | ffmpeg accepts 1.75x chain=%s | rubberband strings=%s"
               % ("; ".join(chain_detail), ffmpeg_ops.default_engine(), accepted,
                  "ok" if rb_ok else "WRONG"))

        # ------------------------------------------------- definition-of-done check
        print("\nTEST 7 (definition of done): pitch is unchanged, measured not assumed",
              flush=True)
        if out15 and os.path.exists(out15):
            f_src = fundamental_hz(clip)
            f_out = fundamental_hz(out15)
            naive = SINE_HZ * 1.5
            ok = abs(f_out - SINE_HZ) <= 5 and abs(f_out - naive) > 50
            record("7. pitch preserved at 1.5x", ok,
                   "source %.0f Hz -> output %.0f Hz (naive resample would give %.0f Hz)"
                   % (f_src, f_out, naive))
        else:
            record("7. pitch preserved at 1.5x", False, "no 1.5x output to measure")

        # ---------------------------------------------------------------- test 8
        print("\nTEST 8: batch of 6 with per-row overrides (definition of done)", flush=True)
        res = upload(small_clips)
        batch8 = res["batch_id"]
        ids = res["job_ids"]
        for job_id in ids:                       # master: everything to 1.5x
            post_json("/api/jobs/" + job_id, {"speed": 1.5}, method="PATCH")
        for job_id in ids[:2]:                   # then override two rows to 1.25x
            post_json("/api/jobs/" + job_id, {"speed": 1.25}, method="PATCH")
        post_json("/api/batches/%s/start" % batch8)
        state8 = wait_for_batch(batch8)

        done = [j for j in state8["jobs"] if j["status"] == "done"]
        speeds = sorted(j["speed"] for j in state8["jobs"])
        names_ok = all(("__1.25x.mp4" if j["speed"] == 1.25 else "__1.5x.mp4")
                       in (j["out_path"] or "") for j in done)
        overrides_held = speeds == [1.25, 1.25, 1.5, 1.5, 1.5, 1.5]
        durations_ok = True
        for j in done:
            info = probe_out(j["out_path"])
            if abs(info["duration"] - 3.0 / j["speed"]) > 0.15:
                durations_ok = False
        record("8. batch with per-row overrides",
               len(done) == 6 and names_ok and overrides_held and durations_ok,
               "%d/6 delivered, speeds %s, filenames %s, durations %s"
               % (len(done), speeds, "ok" if names_ok else "WRONG",
                  "ok" if durations_ok else "WRONG"))

        # ---------------------------------------------------------------- test 9
        print("\nTEST 9: loudness matching (measure, compare, correct)", flush=True)
        state9 = process([quiet], 1.5, match_loudness=True)
        job9 = state9["jobs"][0]
        loud = (job9.get("qa") or {}).get("loudness") or {}
        measured = loud.get("src_lufs") is not None and loud.get("out_lufs") is not None

        # Exercise the correction itself: a known gain must move measured LUFS by it.
        gain_ok = False
        gain_detail = "not run"
        if job9.get("out_path"):
            before = ffmpeg_ops.measure_lufs(job9["out_path"])
            louder = os.path.join(SCRATCH, "gain_check.mp4")
            if ffmpeg_ops.apply_gain(job9["out_path"], louder, 3.0, "gain_check"):
                after = ffmpeg_ops.measure_lufs(louder)
                if before is not None and after is not None:
                    gain_ok = abs((after - before) - 3.0) < 0.6
                    gain_detail = ("%.2f -> %.2f LUFS after +3 dB (delta %.2f)"
                                   % (before, after, after - before))
        record("9. loudness match path",
               job9["status"] == "done" and measured and gain_ok,
               "status=%s, batch measurement=%s, gain check: %s"
               % (job9["status"], json.dumps(loud), gain_detail))

        # --------------------------------------------------------------- test 10
        print("\nTEST 10: QA gate rejects bad output (it is not a rubber stamp)", flush=True)
        from app import qa as qa_mod
        good = job15.get("out_path")
        sub = []

        # Right file, wrong claimed speed -> duration check must bite.
        r = qa_mod.run_qa(good, src["duration"], 2.0, 60, True)
        sub.append(("wrong-speed rejected",
                    not r["passed"] and not _named(r, "duration_match")))

        # Right file, wrong claimed fps -> fps check must bite.
        r = qa_mod.run_qa(good, src["duration"], 1.5, 30, True)
        sub.append(("wrong-fps rejected", not r["passed"] and not _named(r, "fps_match")))

        # Truncated garbage -> must not parse.
        broken = os.path.join(SCRATCH, "broken.mp4")
        with open(broken, "wb") as fh:
            fh.write(b"not a video" * 100)
        r = qa_mod.run_qa(broken, src["duration"], 1.5, 60, True)
        sub.append(("unparseable rejected", not r["passed"]))

        # Missing file -> must not pass.
        r = qa_mod.run_qa(os.path.join(SCRATCH, "nope.mp4"), src["duration"], 1.5, 60, True)
        sub.append(("missing file rejected", not r["passed"]))

        # And the genuine article still passes.
        r = qa_mod.run_qa(good, src["duration"], 1.5, 60, True)
        sub.append(("good file still passes", r["passed"]))

        record("10. QA gate rejects bad output", all(ok for _, ok in sub),
               ", ".join("%s=%s" % (n, "ok" if ok else "FAIL") for n, ok in sub))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + "=" * 74)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print("  %-40s %s" % (name, "PASS" if ok else "FAIL"))
    print("  %d/%d passed" % (passed, len(results)))
    print("=" * 74)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
