"""UPSURGE SpeedLab - FastAPI routes. Local, single user, no auth."""
import base64
import json
import os
import platform
import secrets
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, ffmpeg_ops, probe, worker

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

ALLOWED_SPEEDS = [1.1, 1.25, 1.4, 1.5, 1.75, 2.0]
ALLOWED_PROFILES = ["voice_music", "voice_only"]
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm"}
DEFAULT_SPEED = 1.25
DEFAULT_PROFILE = "voice_music"

# --- exposure controls ------------------------------------------------------
# Unset by default: a local-only run stays frictionless. Set it and every request
# must authenticate, which is what makes reaching this over a public URL sane.
AUTH_TOKEN = os.environ.get("SPEEDLAB_AUTH_TOKEN", "").strip()
MAX_UPLOAD_BYTES = int(os.environ.get("SPEEDLAB_MAX_UPLOAD_MB", "4096")) * 1024 * 1024
MAX_FILES_PER_UPLOAD = int(os.environ.get("SPEEDLAB_MAX_FILES", "50"))
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

@asynccontextmanager
async def lifespan(app):
    db.init_db()
    ffmpeg_ops.detect_capabilities(refresh=True)
    worker.start_worker()
    yield


app = FastAPI(title="UPSURGE SpeedLab", docs_url=None, redoc_url=None, lifespan=lifespan)


def _authorized(request):
    """Basic auth (any username) or a bearer token. Constant-time comparison."""
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return secrets.compare_digest(header[7:].strip(), AUTH_TOKEN)
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, password = decoded.partition(":")
        return secrets.compare_digest(password, AUTH_TOKEN)
    return False


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if AUTH_TOKEN and not _authorized(request):
        return Response(status_code=401, content="authentication required\n",
                        headers={"WWW-Authenticate": 'Basic realm="UPSURGE SpeedLab"'})
    return await call_next(request)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/capabilities")
def capabilities():
    caps = ffmpeg_ops.detect_capabilities()
    return {
        "rubberband": caps["rubberband"],
        "ffmpeg_version": caps["ffmpeg_version"],
        "ffmpeg_available": caps["ffmpeg_available"],
        "engine": caps["engine"],
        "forced_atempo": caps["forced_atempo"],
        "ffmpeg_path": caps["ffmpeg_path"],
        "ffprobe_path": caps["ffprobe_path"],
        "binary_source": caps["binary_source"],
        "scanned": caps["scanned"],
        "auth_enabled": bool(AUTH_TOKEN),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_files": MAX_FILES_PER_UPLOAD,
        "speeds": ALLOWED_SPEEDS,
        "profiles": ALLOWED_PROFILES,
    }


@app.post("/api/capabilities/rescan")
def rescan_capabilities():
    """Re-detect the ffmpeg binaries - useful right after installing a new build."""
    return ffmpeg_ops.detect_capabilities(refresh=True)


# ---------------------------------------------------------------------- ingest

def _save_upload(upload_file, dest):
    """Stream to disk, aborting past the size cap so a big POST can't fill the disk."""
    written = 0
    over = False
    with open(dest, "wb") as out:
        while True:
            chunk = upload_file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                over = True
                break
            out.write(chunk)
    if over:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise ValueError("file exceeds the %d MB upload limit"
                         % (MAX_UPLOAD_BYTES // (1024 * 1024)))
    return written


def _safe_name(name):
    base = os.path.basename(name or "upload")
    return "".join(c for c in base if c.isalnum() or c in " ._-()[]").strip() or "upload"


@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...), match_loudness: str = Form("0"),
                 batch_id: str = Form("")):
    if not files:
        raise HTTPException(400, "no files received")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(413, "too many files in one upload (limit %d)"
                            % MAX_FILES_PER_UPLOAD)

    db.ensure_dirs()
    caps = ffmpeg_ops.detect_capabilities()

    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (batch_id,)) if batch_id else None
    if batch is None:
        batch_id = uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO batches (id, created_at, status, match_loudness) VALUES (?, ?, ?, ?)",
            (batch_id, db.now_iso(), "pending",
             1 if str(match_loudness) in ("1", "true", "True", "on") else 0),
        )

    created = []
    for upload_file in files:
        job_id = uuid.uuid4().hex[:12]
        src_name = _safe_name(upload_file.filename)
        ext = os.path.splitext(src_name)[1].lower()
        dest = os.path.join(db.INBOX, "%s__%s" % (job_id, src_name))

        row = {
            "id": job_id, "batch_id": batch_id, "src_path": dest, "src_name": src_name,
            "sha256": None, "probe_json": "{}", "kind": None, "speed": DEFAULT_SPEED,
            "audio_profile": DEFAULT_PROFILE, "target_fps": None, "engine": caps["engine"],
            "status": "queued", "stage": "probing", "progress": 0.0, "out_path": None,
            "qa_json": None, "error": None, "created_at": db.now_iso(), "force": 0,
            "fps_mode": "auto",
        }

        try:
            _save_upload(upload_file, dest)
        except (ValueError, OSError) as exc:
            row.update(status="failed", stage=None, error=str(exc))
            _insert_job(row)
            created.append(job_id)
            continue
        finally:
            await upload_file.close()

        if ext not in ALLOWED_EXT:
            row.update(status="failed", stage=None,
                       error="unsupported file type '%s' (accepts .mp4 .mov .mkv .webm)" % ext)
            _insert_job(row)
            created.append(job_id)
            continue

        try:
            sha = probe.sha256_file(dest)
            info = probe.probe(dest)
        except Exception as exc:
            row.update(status="failed", stage=None, error="probe failed: %s" % exc)
            _insert_job(row)
            created.append(job_id)
            continue

        row["sha256"] = sha
        row["probe_json"] = json.dumps(info)
        row["kind"] = probe.classify(info)
        row["target_fps"] = ffmpeg_ops.compute_target_fps(DEFAULT_SPEED, info["fps"])
        row["stage"] = None

        # Speeding up an already-sped-up file compounds quality loss and fails silently.
        dup = db.find_output_hash(sha)
        if dup is not None:
            row.update(status="skipped_duplicate",
                       error="This file was already produced by SpeedLab. "
                             "Processing it again will compound quality loss.")

        _insert_job(row)
        db.record_hash(sha, "input", job_id)
        probe.make_thumbnail(dest, job_id, info["duration"])
        created.append(job_id)

    return {"batch_id": batch_id, "job_ids": created}


def _insert_job(row):
    cols = list(row.keys())
    db.execute(
        "INSERT INTO jobs (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
        [row[c] for c in cols],
    )


# ------------------------------------------------------------------------ state

def _job_public(row):
    out = dict(row)
    out["probe"] = json.loads(row["probe_json"] or "{}")
    out["qa"] = json.loads(row["qa_json"]) if row["qa_json"] else None
    out.pop("probe_json", None)
    out.pop("qa_json", None)
    out["has_thumb"] = os.path.exists(os.path.join(db.THUMBS, "%s.jpg" % row["id"]))
    return out


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if batch is None:
        raise HTTPException(404, "no such batch")
    jobs = db.query("SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at, rowid",
                    (batch_id,))
    return {
        "batch": dict(batch),
        "jobs": [_job_public(j) for j in jobs],
        "cancelled": worker.is_cancelled(batch_id),
        "current_job": worker.current_job_id(),
    }


@app.patch("/api/batches/{batch_id}")
async def patch_batch(batch_id: str, request: Request):
    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if batch is None:
        raise HTTPException(404, "no such batch")
    body = await request.json()
    if "match_loudness" in body:
        db.execute("UPDATE batches SET match_loudness = ? WHERE id = ?",
                   (1 if body["match_loudness"] else 0, batch_id))
    return {"ok": True}


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, request: Request):
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise HTTPException(404, "no such job")
    if job["status"] == "running":
        raise HTTPException(409, "job is running")

    body = await request.json()
    fields = {}

    if "speed" in body:
        speed = float(body["speed"])
        if not any(abs(speed - s) < 1e-6 for s in ALLOWED_SPEEDS):
            raise HTTPException(400, "speed must be one of %s" % ALLOWED_SPEEDS)
        fields["speed"] = speed

    if "audio_profile" in body:
        if body["audio_profile"] not in ALLOWED_PROFILES:
            raise HTTPException(400, "audio_profile must be one of %s" % ALLOWED_PROFILES)
        fields["audio_profile"] = body["audio_profile"]

    if "fps_mode" in body:
        mode = str(body["fps_mode"])
        if mode not in ("auto", "30", "60"):
            raise HTTPException(400, "fps_mode must be auto, 30 or 60")
        fields["fps_mode"] = mode
        if mode != "auto":
            fields["target_fps"] = int(mode)
    elif "target_fps" in body:
        fields["target_fps"] = int(body["target_fps"])
        fields["fps_mode"] = str(int(body["target_fps"]))

    # Recompute the auto fps whenever speed moves and the row is still on auto.
    mode = fields.get("fps_mode", job["fps_mode"] or "auto")
    if mode == "auto" and "speed" in fields:
        info = json.loads(job["probe_json"] or "{}")
        fields["target_fps"] = ffmpeg_ops.compute_target_fps(fields["speed"],
                                                             info.get("fps", 30))

    if body.get("force"):
        fields["force"] = 1
        if job["status"] == "skipped_duplicate":
            fields["status"] = "queued"
            fields["error"] = None

    if body.get("retry") and job["status"] == "failed":
        fields["status"] = "queued"
        fields["error"] = None
        fields["progress"] = 0.0

    if fields:
        db.update_job(job_id, **fields)
    return {"ok": True, "job": _job_public(db.query_one("SELECT * FROM jobs WHERE id = ?",
                                                        (job_id,)))}


@app.post("/api/batches/{batch_id}/start")
def start_batch(batch_id: str):
    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if batch is None:
        raise HTTPException(404, "no such batch")
    worker.clear_cancel(batch_id)
    jobs = db.query("SELECT id FROM jobs WHERE batch_id = ? AND status = 'queued' "
                    "ORDER BY created_at, rowid", (batch_id,))
    for j in jobs:
        worker.enqueue(j["id"])
    db.execute("UPDATE batches SET status = ? WHERE id = ?",
               ("running" if jobs else batch["status"], batch_id))
    return {"queued": len(jobs)}


def _under(path, *roots):
    """Guard: only ever unlink things inside SpeedLab's own directories."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return any(real.startswith(os.path.realpath(r) + os.sep) for r in roots)


def _purge_job(job):
    """Delete a job's working files. A delivered outbox file is never touched."""
    job_id = job["id"]
    removed = []
    candidates = [job["src_path"],
                  os.path.join(db.THUMBS, "%s.jpg" % job_id),
                  os.path.join(db.WORK, "%s.mp4" % job_id)]
    # A QA-failed job's out_path points into work/, not outbox/ - that one goes too.
    if job["out_path"] and _under(job["out_path"], db.WORK):
        candidates.append(job["out_path"])

    for path in candidates:
        if path and os.path.exists(path) and _under(path, db.INBOX, db.WORK):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass

    # Drop the input-hash record so the same file can be re-added later. Output
    # hashes stay: removing one would disarm duplicate protection for that file.
    db.execute("DELETE FROM processed_hashes WHERE job_id = ? AND role = 'input'", (job_id,))
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return removed


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise HTTPException(404, "no such job")
    if job["status"] == "running":
        raise HTTPException(409, "job is running - cancel the batch first")
    kept = job["out_path"] if job["out_path"] and _under(job["out_path"], db.OUTBOX) else None
    removed = _purge_job(job)
    return {"ok": True, "removed": removed, "kept_output": kept}


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str):
    if db.query_one("SELECT id FROM batches WHERE id = ?", (batch_id,)) is None:
        raise HTTPException(404, "no such batch")
    jobs = db.query("SELECT * FROM jobs WHERE batch_id = ?", (batch_id,))
    running = [j for j in jobs if j["status"] == "running"]
    if running:
        raise HTTPException(409, "a job is running - cancel the batch first")
    for job in jobs:
        _purge_job(job)
    db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    worker.clear_cancel(batch_id)
    return {"ok": True, "deleted": len(jobs)}


@app.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str):
    if db.query_one("SELECT id FROM batches WHERE id = ?", (batch_id,)) is None:
        raise HTTPException(404, "no such batch")
    worker.cancel_batch(batch_id)
    return {"ok": True, "note": "stops after the current job finishes; "
                                "remaining jobs stay queued"}


# ------------------------------------------------------------------------ assets

@app.get("/api/thumb/{job_id}")
def thumb(job_id: str):
    path = os.path.join(db.THUMBS, "%s.jpg" % job_id)
    if not os.path.exists(path):
        raise HTTPException(404, "no thumbnail")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/output/{job_id}")
def output(job_id: str):
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None or not job["out_path"] or not os.path.exists(job["out_path"]):
        raise HTTPException(404, "no output file")
    return FileResponse(job["out_path"], media_type="video/mp4",
                        filename=os.path.basename(job["out_path"]))


@app.post("/api/reveal/{job_id}")
def reveal(job_id: str, request: Request):
    """Open the containing folder in the OS file browser.

    This runs a command on the machine hosting SpeedLab, so it is refused for any
    non-loopback caller - a remote user must never be able to drive the host's shell.
    """
    client = request.client.host if request.client else None
    if client not in LOOPBACK:
        raise HTTPException(403, "reveal is only available to a local session")
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None or not job["out_path"] or not os.path.exists(job["out_path"]):
        raise HTTPException(404, "no output file")
    path = job["out_path"]
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "path": path}


@app.get("/api/log/{job_id}")
def get_log(job_id: str):
    path = os.path.join(db.LOGS, "%s.log" % job_id)
    if not os.path.exists(path):
        raise HTTPException(404, "no log yet")
    return FileResponse(path, media_type="text/plain")
