"""Single background thread, strictly sequential. One job at a time, in queue order."""
import csv
import json
import os
import queue
import shutil
import threading
import traceback

from . import db, ffmpeg_ops, probe, qa

_q = queue.Queue()
_thread = None
_cancelled = set()
_cancel_lock = threading.Lock()
_current_job = {"id": None}

MANIFEST_HEADER = [
    "timestamp", "job_id", "src_name", "src_duration", "out_duration",
    "speed", "engine", "audio_profile", "target_fps", "qa_passed", "out_path",
]


# ------------------------------------------------------------------- queue control

def start_worker():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="speedlab-worker", daemon=True)
    _thread.start()


def enqueue(job_id):
    _q.put(job_id)


def cancel_batch(batch_id):
    with _cancel_lock:
        _cancelled.add(batch_id)


def is_cancelled(batch_id):
    with _cancel_lock:
        return batch_id in _cancelled


def clear_cancel(batch_id):
    with _cancel_lock:
        _cancelled.discard(batch_id)


def current_job_id():
    return _current_job["id"]


def _loop():
    while True:
        job_id = _q.get()
        try:
            job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if job is None or job["status"] != "queued":
                continue
            if is_cancelled(job["batch_id"]):
                # "Stop after current job finishes" - leave the rest queued so the
                # batch can simply be started again.
                _refresh_batch_status(job["batch_id"])
                continue
            _current_job["id"] = job_id
            process_job(job)
        except Exception:
            tb = traceback.format_exc()
            try:
                ffmpeg_ops.append_log(job_id, "\n=== worker crash ===\n" + tb)
                db.update_job(job_id, status="failed", stage=None, progress=0.0,
                              error="worker error: %s" % tb.strip().splitlines()[-1])
            except Exception:
                pass
        finally:
            _current_job["id"] = None
            try:
                job = db.query_one("SELECT batch_id FROM jobs WHERE id = ?", (job_id,))
                if job:
                    _refresh_batch_status(job["batch_id"])
            except Exception:
                pass
            _q.task_done()


def _refresh_batch_status(batch_id):
    rows = db.query("SELECT status FROM jobs WHERE batch_id = ?", (batch_id,))
    statuses = [r["status"] for r in rows]
    if any(s == "running" for s in statuses):
        status = "running"
    elif any(s == "queued" for s in statuses):
        status = "running" if not is_cancelled(batch_id) else "pending"
    elif any(s == "failed" for s in statuses):
        status = "failed"
    else:
        status = "done"
    db.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch_id))


# ------------------------------------------------------------------------ pipeline

def process_job(job):
    job_id = job["id"]
    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (job["batch_id"],))
    match_loudness = bool(batch and batch["match_loudness"])

    info = json.loads(job["probe_json"] or "{}")
    src = job["src_path"]
    speed = float(job["speed"])
    has_audio = bool(info.get("has_audio"))
    src_duration = float(info.get("duration") or 0.0)

    target_fps = int(job["target_fps"] or ffmpeg_ops.compute_target_fps(speed, info.get("fps", 30)))
    engine = ffmpeg_ops.default_engine()

    db.update_job(job_id, status="running", stage="stretching", progress=0.0,
                  engine=engine, target_fps=target_fps, error=None)

    audio_filter = ffmpeg_ops.build_audio_filter(engine, speed, job["audio_profile"]) if has_audio else ""
    work_out = os.path.join(db.WORK, "%s.mp4" % job_id)
    expected_duration = (src_duration / speed) if speed else 0.0

    ffmpeg_ops.append_log(job_id, "job %s | %s | speed %s | engine %s | profile %s | "
                                  "target_fps %d | audio %s"
                          % (job_id, job["src_name"], ffmpeg_ops.fmt_num(speed), engine,
                             job["audio_profile"], target_fps,
                             "yes" if has_audio else "none"))

    cmd = ffmpeg_ops.build_command(src, work_out, speed, target_fps, audio_filter, has_audio)

    state = {"stage": "stretching"}

    def on_progress(frac):
        if state["stage"] != "encoding":
            state["stage"] = "encoding"
            db.update_job(job_id, stage="encoding")
        db.update_job(job_id, progress=round(frac, 4))

    code, stderr = ffmpeg_ops.run_with_progress(cmd, job_id, expected_duration,
                                                on_progress, label="encode")
    if code != 0 or not os.path.exists(work_out):
        db.update_job(job_id, status="failed", stage=None, progress=0.0,
                      error=_last_error(stderr) or "ffmpeg exited with code %d" % code)
        return

    db.update_job(job_id, stage="verifying", progress=0.99)

    # Loudness matching is opt-in per batch. Time-stretching is roughly loudness
    # neutral, so the default is to leave levels alone.
    loudness = None
    if match_loudness and has_audio:
        loudness = _match_loudness(job_id, src, work_out)

    result = qa.run_qa(work_out, src_duration, speed, target_fps, has_audio)
    if loudness:
        result["loudness"] = loudness

    if not result["passed"]:
        failed = [c["name"] for c in result["checks"] if not c["passed"]]
        db.update_job(job_id, status="failed", stage=None, progress=1.0,
                      qa_json=json.dumps(result),
                      out_path=work_out,
                      error="QA failed: %s (file kept in work/)" % ", ".join(failed))
        ffmpeg_ops.append_log(job_id, "\n=== QA FAILED ===\n" + json.dumps(result, indent=2))
        return

    final = _deliver(job, work_out, speed)
    out_sha = probe.sha256_file(final)
    db.record_hash(out_sha, "output", job_id)

    db.update_job(job_id, status="done", stage=None, progress=1.0,
                  out_path=final, qa_json=json.dumps(result), error=None)
    ffmpeg_ops.append_log(job_id, "\n=== QA PASSED ===\n" + json.dumps(result, indent=2)
                          + "\ndelivered: %s\noutput sha256: %s" % (final, out_sha))
    _append_manifest(job, result, final, engine, target_fps, src_duration)


def _match_loudness(job_id, src, out):
    src_i = ffmpeg_ops.measure_lufs(src, job_id)
    out_i = ffmpeg_ops.measure_lufs(out, job_id)
    if src_i is None or out_i is None:
        return {"applied": False, "reason": "could not measure integrated loudness",
                "src_lufs": src_i, "out_lufs": out_i}
    delta = src_i - out_i
    if abs(delta) <= 0.5:
        return {"applied": False, "reason": "within 0.5 LU", "src_lufs": round(src_i, 2),
                "out_lufs": round(out_i, 2), "delta_lu": round(delta, 2)}
    tmp = out + ".gain.mp4"
    ok = ffmpeg_ops.apply_gain(out, tmp, delta, job_id)
    if ok:
        shutil.move(tmp, out)
    elif os.path.exists(tmp):
        os.remove(tmp)
    return {"applied": bool(ok), "src_lufs": round(src_i, 2), "out_lufs": round(out_i, 2),
            "delta_lu": round(delta, 2),
            "reason": None if ok else "gain remux failed"}


def _deliver(job, work_out, speed):
    stem = os.path.splitext(job["src_name"])[0]
    name = "%s__%sx.mp4" % (stem, ffmpeg_ops.fmt_num(speed))
    dest = os.path.join(db.OUTBOX, name)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(db.OUTBOX, "%s__%sx_%d.mp4" % (stem, ffmpeg_ops.fmt_num(speed), n))
        n += 1
    shutil.move(work_out, dest)
    return dest


def _append_manifest(job, result, out_path, engine, target_fps, src_duration):
    new = not os.path.exists(db.MANIFEST_CSV)
    with open(db.MANIFEST_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(MANIFEST_HEADER)
        w.writerow([
            db.now_iso(), job["id"], job["src_name"], round(src_duration, 3),
            result.get("out_duration"), ffmpeg_ops.fmt_num(job["speed"]), engine,
            job["audio_profile"], target_fps, "yes" if result["passed"] else "no", out_path,
        ])


def _last_error(stderr):
    """Pull the most useful line out of ffmpeg's stderr for the UI row."""
    if not stderr:
        return None
    lines = [l.strip() for l in stderr.strip().splitlines() if l.strip()]
    for line in reversed(lines):
        low = line.lower()
        if ("error" in low or "invalid" in low or "no such" in low
                or "failed" in low or "unable" in low):
            return line[:300]
    return lines[-1][:300] if lines else None
