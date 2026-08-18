"""SQLite schema and connection helpers. Plain sqlite3, no ORM, kept inspectable."""
import os
import sqlite3
import threading
from datetime import datetime, timezone

# SPEEDLAB_ROOT lets the self-test run against a scratch tree instead of the
# real inbox/work/outbox.
PROJECT_ROOT = os.environ.get(
    "SPEEDLAB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(PROJECT_ROOT, "speedlab.db")

INBOX = os.path.join(PROJECT_ROOT, "inbox")
WORK = os.path.join(PROJECT_ROOT, "work")
THUMBS = os.path.join(WORK, "thumbs")
OUTBOX = os.path.join(PROJECT_ROOT, "outbox")
LOGS = os.path.join(PROJECT_ROOT, "logs")
MANIFESTS = os.path.join(PROJECT_ROOT, "manifests")
MANIFEST_CSV = os.path.join(MANIFESTS, "manifest.csv")

# sqlite handles concurrent readers fine, but this app has exactly one writer
# thread plus the request thread. A single lock around writes keeps it simple.
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created_at TEXT,
  status TEXT,
  match_loudness INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  src_path TEXT,
  src_name TEXT,
  sha256 TEXT,
  probe_json TEXT,
  kind TEXT,
  speed REAL,
  audio_profile TEXT,
  target_fps INTEGER,
  engine TEXT,
  status TEXT,
  stage TEXT,
  progress REAL,
  out_path TEXT,
  qa_json TEXT,
  error TEXT,
  created_at TEXT,
  force INTEGER DEFAULT 0,
  fps_mode TEXT DEFAULT 'auto'
);

CREATE TABLE IF NOT EXISTS processed_hashes (
  sha256 TEXT PRIMARY KEY,
  role TEXT,
  job_id TEXT,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
"""


def ensure_dirs():
    for d in (INBOX, WORK, THUMBS, OUTBOX, LOGS, MANIFESTS):
        os.makedirs(d, exist_ok=True)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    ensure_dirs()
    with _write_lock:
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def query(sql, params=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with _write_lock:
        conn = connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def update_job(job_id, **fields):
    """Partial update on a job row. Unknown keys are rejected loudly."""
    allowed = {
        "src_path", "src_name", "sha256", "probe_json", "kind", "speed",
        "audio_profile", "target_fps", "engine", "status", "stage", "progress",
        "out_path", "qa_json", "error", "force", "fps_mode",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError("unknown job fields: %s" % ", ".join(sorted(bad)))
    if not fields:
        return 0
    sets = ", ".join("%s = ?" % k for k in fields)
    params = list(fields.values()) + [job_id]
    return execute("UPDATE jobs SET %s WHERE id = ?" % sets, params)


def record_hash(sha256, role, job_id):
    """Record a hash. An existing 'output' row is never downgraded to 'input' -
    that would quietly destroy the duplicate protection for that file."""
    existing = query_one("SELECT role FROM processed_hashes WHERE sha256 = ?", (sha256,))
    if existing is not None and existing["role"] == "output" and role != "output":
        return
    execute(
        "INSERT OR REPLACE INTO processed_hashes (sha256, role, job_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        (sha256, role, job_id, now_iso()),
    )


def find_output_hash(sha256):
    return query_one(
        "SELECT * FROM processed_hashes WHERE sha256 = ? AND role = 'output'", (sha256,)
    )
