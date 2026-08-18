"""UPSURGE SpeedLab - FastAPI routes. Local, single user, no auth."""
import base64
import json
import os
import re
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

# A reverse proxy or tunnel (cloudflared, ngrok, nginx) opens its own connection to
# the app, so the socket's peer address is the PROXY, not the visitor. On a natively
# run instance that peer IS 127.0.0.1, which would make a plain loopback check say
# "local" for requests arriving from the public internet. Verified: a proxy
# connecting from 127.0.0.1 sails straight past a naive client.host check.
# So a request is only treated as local when the socket is loopback AND it carries
# no sign of having been forwarded - and SPEEDLAB_PUBLIC turns it off outright.
PROXY_HEADERS = (
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
    "forwarded", "cf-connecting-ip", "cf-ray", "cf-ipcountry", "cf-visitor",
    "x-original-forwarded-for", "true-client-ip", "fly-client-ip",
    "ngrok-trace-id", "x-nginx-proxy",
)
PUBLIC_MODE = os.environ.get("SPEEDLAB_PUBLIC", "").strip().lower() not in ("", "0", "false", "no")

# Every visitor gets an opaque session id in a cookie. This is NOT a login - it is
# invisible and needs no credentials - but it scopes each visitor to the batches
# they created. Without it, ids are the only protection, and an id that leaks
# anywhere hands a stranger the owner's media.
SID_COOKIE = "speedlab_sid"
SID_RE = re.compile(r"^[0-9a-f]{32}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# An exposed instance gets stricter defaults automatically: it is serving strangers.
MAX_TOTAL_DISK_MB = _env_int("SPEEDLAB_MAX_TOTAL_DISK_MB", 20480 if PUBLIC_MODE else 0)
MAX_INPUT_SECONDS = _env_int("SPEEDLAB_MAX_INPUT_SECONDS", 1800 if PUBLIC_MODE else 0)
MAX_INPUT_PIXELS = _env_int("SPEEDLAB_MAX_INPUT_PIXELS", 4096 * 4096 if PUBLIC_MODE else 0)

@asynccontextmanager
async def lifespan(app):
    db.init_db()
    ffmpeg_ops.detect_capabilities(refresh=True)
    worker.start_worker()
    yield


app = FastAPI(title="UPSURGE SpeedLab", docs_url=None, redoc_url=None, lifespan=lifespan)


def is_local_session(request):
    """True only for a request that genuinely originated on this machine.

    Deliberately conservative: anything that looks proxied is treated as remote.
    """
    if PUBLIC_MODE:
        return False
    for header in PROXY_HEADERS:
        if header in request.headers:
            return False
    client = request.client.host if request.client else None
    return client in LOOPBACK


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
async def session_and_auth(request: Request, call_next):
    if AUTH_TOKEN and not _authorized(request):
        return Response(status_code=401, content="authentication required\n",
                        headers={"WWW-Authenticate": 'Basic realm="UPSURGE SpeedLab"'})

    sid = request.cookies.get(SID_COOKIE) or ""
    issue = not SID_RE.match(sid)
    if issue:
        sid = uuid.uuid4().hex
    request.state.sid = sid

    response = await call_next(request)
    if issue:
        response.set_cookie(SID_COOKIE, sid, max_age=60 * 60 * 24 * 365,
                            httponly=True, samesite="lax", path="/")
    return response


def _sid(request):
    return getattr(request.state, "sid", "") or ""


def owns_batch(request, batch):
    """A batch belongs to the session that created it.

    Rows predating session ownership have no owner; those stay reachable, but only
    from a genuinely local session, never from an exposed one.
    """
    if batch is None:
        return False
    owner = batch["owner_sid"] if "owner_sid" in batch.keys() else None
    if not owner:
        return is_local_session(request)
    return secrets.compare_digest(str(owner), _sid(request))


def batch_or_404(request, batch_id):
    """404 rather than 403 on a mismatch: do not confirm that an id exists."""
    if not ID_RE.match(batch_id or ""):
        raise HTTPException(404, "no such batch")
    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if not owns_batch(request, batch):
        raise HTTPException(404, "no such batch")
    return batch


def job_or_404(request, job_id):
    """Resolve a job only if the caller owns the batch it belongs to.

    The id format is validated first: these ids reach the filesystem in
    thumb/log paths, and an unvalidated one is a traversal primitive.
    """
    if not ID_RE.match(job_id or ""):
        raise HTTPException(404, "no such job")
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise HTTPException(404, "no such job")
    batch = db.query_one("SELECT * FROM batches WHERE id = ?", (job["batch_id"],))
    if not owns_batch(request, batch):
        raise HTTPException(404, "no such job")
    return job


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/capabilities")
def capabilities(request: Request):
    caps = ffmpeg_ops.detect_capabilities()
    local = is_local_session(request)
    return {
        "rubberband": caps["rubberband"],
        "ffmpeg_version": caps["ffmpeg_version"],
        "ffmpeg_available": caps["ffmpeg_available"],
        "engine": caps["engine"],
        "forced_atempo": caps["forced_atempo"],
        "ffmpeg_path": caps["ffmpeg_path"] if local else os.path.basename(caps["ffmpeg_path"]),
        "ffprobe_path": caps["ffprobe_path"] if local else os.path.basename(caps["ffprobe_path"]),
        "binary_source": caps["binary_source"],
        "scanned": caps["scanned"] if local else [],
        "auth_enabled": bool(AUTH_TOKEN),
        "public_mode": PUBLIC_MODE,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_files": MAX_FILES_PER_UPLOAD,
        "reveal_available": local,
        "max_input_seconds": MAX_INPUT_SECONDS,
        "speeds": ALLOWED_SPEEDS,
        "profiles": ALLOWED_PROFILES,
    }


@app.post("/api/capabilities/rescan")
def rescan_capabilities(request: Request):
    """Re-detect the ffmpeg binaries - useful right after installing a new build."""
    if not is_local_session(request):
        raise HTTPException(403, "rescan is only available to a local session")
    return ffmpeg_ops.detect_capabilities(refresh=True)


# ---------------------------------------------------------------------- ingest

def _tree_size(*roots):
    total = 0
    for root in roots:
        for dirpath, _, names in os.walk(root):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    return total


def _discard(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _admission_refusal(info):
    if MAX_INPUT_SECONDS and info.get("duration", 0) > MAX_INPUT_SECONDS:
        return ("source is %.0fs, over the %ds limit for this instance"
                % (info.get("duration", 0), MAX_INPUT_SECONDS))
    pixels = int(info.get("width") or 0) * int(info.get("height") or 0)
    if MAX_INPUT_PIXELS and pixels > MAX_INPUT_PIXELS:
        return ("source is %dx%d, over the frame-size limit for this instance"
                % (info.get("width", 0), info.get("height", 0)))
    return None


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
async def upload(request: Request, files: List[UploadFile] = File(...),
                 match_loudness: str = Form("0"), batch_id: str = Form("")):
    if not files:
        raise HTTPException(400, "no files received")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(413, "too many files in one upload (limit %d)"
                            % MAX_FILES_PER_UPLOAD)

    db.ensure_dirs()
    caps = ffmpeg_ops.detect_capabilities()

    batch = None
    if batch_id and ID_RE.match(batch_id):
        candidate = db.query_one("SELECT * FROM batches WHERE id = ?", (batch_id,))
        if owns_batch(request, candidate):
            batch = candidate
    if batch is None:
        batch_id = uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO batches (id, created_at, status, match_loudness, owner_sid) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, db.now_iso(), "pending",
             1 if str(match_loudness) in ("1", "true", "True", "on") else 0,
             _sid(request)),
        )

    if MAX_TOTAL_DISK_MB:
        used = _tree_size(db.INBOX, db.WORK, db.OUTBOX) // (1024 * 1024)
        if used >= MAX_TOTAL_DISK_MB:
            raise HTTPException(507, "storage full (%d MB of %d MB used); remove some files"
                                % (used, MAX_TOTAL_DISK_MB))

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

        # Checked before a single byte is written: a rejected type should never
        # cost disk, and previously the file was written and then orphaned.
        if ext not in ALLOWED_EXT:
            await upload_file.close()
            row.update(status="failed", stage=None, src_path=None,
                       error="unsupported file type '%s' (accepts .mp4 .mov .mkv .webm)" % ext)
            _insert_job(row)
            created.append(job_id)
            continue

        try:
            _save_upload(upload_file, dest)
        except (ValueError, OSError) as exc:
            row.update(status="failed", stage=None, src_path=None, error=str(exc))
            _insert_job(row)
            created.append(job_id)
            continue
        finally:
            await upload_file.close()

        try:
            sha = probe.sha256_file(dest)
            info = probe.probe(dest)
        except Exception as exc:
            _discard(dest)
            row.update(status="failed", stage=None, src_path=None,
                       error="probe failed: %s" % exc)
            _insert_job(row)
            created.append(job_id)
            continue

        # Admission control: a tiny file can declare an enormous duration or frame
        # size and turn one request into hours of encoding.
        refusal = _admission_refusal(info)
        if refusal:
            _discard(dest)
            row.update(status="failed", stage=None, src_path=None, error=refusal)
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

def _job_public(row, local=False):
    out = dict(row)
    # Absolute paths disclose the install directory and OS username. A local
    # session gets them (it needs them to reveal the file); nobody else does.
    if not local:
        out.pop("src_path", None)
        if out.get("out_path"):
            out["out_path"] = os.path.basename(out["out_path"])
    out["probe"] = json.loads(row["probe_json"] or "{}")
    out["qa"] = json.loads(row["qa_json"]) if row["qa_json"] else None
    out.pop("probe_json", None)
    out.pop("qa_json", None)
    out["has_thumb"] = os.path.exists(os.path.join(db.THUMBS, "%s.jpg" % row["id"]))
    return out


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str, request: Request):
    batch = batch_or_404(request, batch_id)
    jobs = db.query("SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at, rowid",
                    (batch_id,))
    local = is_local_session(request)
    # The worker is process-global, so echoing its job id unscoped told every
    # caller what someone else was processing - which is all an id-secrecy model
    # needs to lose. Only report it when it belongs to this batch.
    running = worker.current_job_id()
    if running and not any(j["id"] == running for j in jobs):
        running = None
    return {
        "batch": dict(batch),
        "jobs": [_job_public(j, local) for j in jobs],
        "cancelled": worker.is_cancelled(batch_id),
        "current_job": running,
    }


@app.patch("/api/batches/{batch_id}")
async def patch_batch(batch_id: str, request: Request):
    batch_or_404(request, batch_id)
    body = await request.json()
    if "match_loudness" in body:
        db.execute("UPDATE batches SET match_loudness = ? WHERE id = ?",
                   (1 if body["match_loudness"] else 0, batch_id))
    return {"ok": True}


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, request: Request):
    job = job_or_404(request, job_id)
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
        # Interpolated straight into -filter_complex, so it takes the same
        # allowlist as fps_mode. An arbitrary integer here is a CPU/disk bomb.
        try:
            fps = int(body["target_fps"])
        except (TypeError, ValueError):
            raise HTTPException(400, "target_fps must be 30 or 60")
        if fps not in (30, 60):
            raise HTTPException(400, "target_fps must be 30 or 60")
        fields["target_fps"] = fps
        fields["fps_mode"] = str(fps)

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
    return {"ok": True,
            "job": _job_public(db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)),
                               is_local_session(request))}


@app.post("/api/batches/{batch_id}/start")
def start_batch(batch_id: str, request: Request):
    batch = batch_or_404(request, batch_id)
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
def delete_job(job_id: str, request: Request):
    job = job_or_404(request, job_id)
    if job["status"] == "running":
        raise HTTPException(409, "job is running - cancel the batch first")
    kept = job["out_path"] if job["out_path"] and _under(job["out_path"], db.OUTBOX) else None
    removed = _purge_job(job)
    return {"ok": True, "removed": removed, "kept_output": kept}


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str, request: Request):
    batch_or_404(request, batch_id)
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
def cancel_batch(batch_id: str, request: Request):
    batch_or_404(request, batch_id)
    worker.cancel_batch(batch_id)
    return {"ok": True, "note": "stops after the current job finishes; "
                                "remaining jobs stay queued"}


# ------------------------------------------------------------------------ assets

@app.get("/api/thumb/{job_id}")
def thumb(job_id: str, request: Request):
    job_or_404(request, job_id)
    path = os.path.join(db.THUMBS, "%s.jpg" % job_id)
    if not os.path.exists(path):
        raise HTTPException(404, "no thumbnail")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/output/{job_id}")
def output(job_id: str, request: Request):
    job = job_or_404(request, job_id)
    if not job["out_path"] or not os.path.exists(job["out_path"]):
        raise HTTPException(404, "no output file")
    return FileResponse(job["out_path"], media_type="video/mp4",
                        filename=os.path.basename(job["out_path"]))


@app.post("/api/reveal/{job_id}")
def reveal(job_id: str, request: Request):
    """Open the containing folder in the OS file browser.

    This runs a command on the machine hosting SpeedLab, so it is refused for any
    non-loopback caller - a remote user must never be able to drive the host's shell.
    """
    if not is_local_session(request):
        raise HTTPException(403, "reveal is only available to a local session")
    job = job_or_404(request, job_id)
    if not job["out_path"] or not os.path.exists(job["out_path"]):
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
def get_log(job_id: str, request: Request):
    # The log carries the absolute source path and original filename, so it is
    # owner-only like the media itself.
    job_or_404(request, job_id)
    path = os.path.join(db.LOGS, "%s.log" % job_id)
    if not os.path.exists(path):
        raise HTTPException(404, "no log yet")
    return FileResponse(path, media_type="text/plain")
