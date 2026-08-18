# UPSURGE SpeedLab

Local batch console for speeding up finished video files **without chipmunk audio**.

Drop 10–12 files in, set one speed for all of them, override individual rows, hit
**Process All**, collect finished files from `outbox/`. Duration changes; pitch does not.

---

## Getting it on another machine

```
git clone https://github.com/rohitdhuriya-debug/upsurge-speedlab.git
cd upsurge-speedlab
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
run.bat
```

(macOS/Linux: `.venv/bin/pip install -r requirements.txt` then `./run.command`.)

The repo carries code only. `inbox/`, `work/`, `outbox/`, `logs/`, `manifests/` and the
SQLite database are deliberately gitignored — your media and processing history stay on your
machine and never reach GitHub.

## Running it

```
run.bat
```

(macOS/Linux: `./run.command`)

That starts Uvicorn on **port 5070** and opens `http://localhost:5070`. First run installs
dependencies if they are missing.

Manual start:

```
python -m uvicorn app.main:app --host 127.0.0.1 --port 5070
```

Requirements: Python 3.9+, FFmpeg on PATH, and `pip install -r requirements.txt`
(`fastapi`, `uvicorn`, `python-multipart` — nothing else).

## Using it

1. Drag files onto the drop zone (`.mp4 .mov .mkv .webm`, multi-select). Each file is
   hashed, probed and thumbnailed on arrival.
2. Set **Set all speed to** and **Set all audio profile to** in the master bar. These write
   into every editable row once. Any per-row change you make afterwards wins — master
   settings are never re-applied on redraw.
3. Tick **Match source loudness** only if you want output levels measured against the
   source (off by default; see below).
4. **Process All**. Jobs run one at a time, in order. **Cancel** stops after the job that
   is currently running; anything still queued stays queued so you can start again.
5. Finished rows show a green tick, the output duration and fps, a **QA detail** panel and
   a link that reveals the file in Explorer/Finder.

### Removing files

Each row has a `×`. It removes the row and deletes the uploaded copy in `inbox/`, its
thumbnail and any working file — but **never** a delivered file in `outbox/`. That is your
output; delete it from Finder/Explorer if you want it gone.

A row that is mid-encode cannot be removed; cancel the batch first. Removing a row also
clears its *input* hash so the same file can be added again later. Output hashes are kept,
because dropping one would silently disarm duplicate protection for that file.

**Remove All** clears the whole batch the same way, telling you first how many finished files
in `outbox/` it is keeping.

### Audio profiles

| Profile | Use for | Filter |
|---|---|---|
| **Voice + music** (default) | Any file with a mixed audio track — voice and music already blended | `transients=mixed:detector=compound` |
| **Voice only** | Files with genuinely no music bed | `transients=smooth:detector=soft` |

`transients=mixed` is a deliberate compromise. Voice wants smooth phase handling,
percussive music wants crisp transient detection, and on a single mixed track you cannot
have both. It is not a setting to "optimise".

### Frame rate

Non-integer speeds on 30 fps source produce uneven frame cadence, which reads as stutter on
motion graphics. So:

- whole-number speed (2.0) → keep source fps
- fractional speed (1.25, 1.4, 1.5, 1.75) → 60 fps

Applied through the `fps` filter with `-fps_mode cfr`. The **fps** column shows the computed
value as `Auto (60)` and lets you force 30 or 60 per row.

### Loudness

Default is **no normalisation** — time-stretching is loudness-neutral in practice (measured
at 0.09 LU drift across a 1.5× pass during the self-test).

`speechnorm` is deliberately **not** used anywhere: it is a speech normaliser, and on a
mixed track it pumps the music bed.

With **Match source loudness** ticked, SpeedLab measures integrated LUFS of the input and of
the encoded output, and if they differ by more than 0.5 LU re-muxes with a fixed
`volume={delta}dB`. The measurements land in the row's QA panel either way.

### Duplicate protection

Speeding up an already-sped-up file is the most destructive accident in this pipeline and it
fails silently. Every output's SHA-256 is recorded. If you drop one back in, the row turns
red and is **not** processed. **Force anyway** overrides it per row if you really mean it.

Note this matches exact file hashes. A re-exported or re-encoded copy of an output is a
different file and will not be caught.

### QA gate

Nothing reaches `outbox/` without passing all five:

1. output exists and `ffprobe` parses it
2. `|actual duration − src/speed| < 0.15 s`
3. video-vs-audio stream drift `< 0.10 s`
4. frame count > 0 and file size > 10 KB
5. output fps matches target within 0.01

A failure leaves the file in `work/`, marks the row red with the reason, and writes the
detail into the row's QA panel. Never a silent bad delivery.

## rubberband vs atempo

SpeedLab prefers FFmpeg's **rubberband** filter. It checks at boot (`GET /api/capabilities`)
and falls back to a chained `atempo` if it is absent, showing a persistent orange banner.

Both preserve pitch. rubberband is better above ~1.5× because it handles formants and
transients explicitly; atempo starts to smear.

### Installing a rubberband-capable FFmpeg on Windows

The stock/essentials builds do **not** include it. You need the gyan.dev **full** build:

1. Download `ffmpeg-release-full.7z` from <https://www.gyan.dev/ffmpeg/builds/>
2. Extract to e.g. `C:\ffmpeg`
3. Put `C:\ffmpeg\bin` on your PATH **ahead of** any existing FFmpeg
4. Open a new terminal and confirm:

```
ffmpeg -filters | findstr rubberband
```

5. Restart SpeedLab. The orange banner disappears and the header reads
   `audio engine: rubberband`.

### Installing a rubberband-capable FFmpeg on macOS

Homebrew's core `ffmpeg` is **not** built with rubberband, and the tap that is uses the same
formula name — so Homebrew refuses to hold both. You have to swap:

```
brew tap homebrew-ffmpeg/ffmpeg
brew uninstall ffmpeg
brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-rubberband
```

That last step builds from source and takes a while. To go back:
`brew uninstall ffmpeg && brew install ffmpeg`.

### How SpeedLab picks its FFmpeg

The first `ffmpeg` on PATH is not always the best one installed. On boot SpeedLab scans the
usual locations — PATH first, then Homebrew prefixes and Cellar on macOS, then
`C:\ffmpeg\bin`, `C:\Program Files\ffmpeg\bin` and any `C:\ffmpeg*\bin` on Windows — and
**uses the first build that actually has rubberband**, falling back to the PATH one if none
does. `ffprobe` is always taken from the same build so the two can never disagree.

So on Windows you can simply extract the gyan.dev full build to `C:\ffmpeg` and restart
SpeedLab; you do not have to touch PATH. Hover the header line to see exactly which binary
was chosen and why. Setting `SPEEDLAB_FFMPEG` overrides the scan entirely and is never
second-guessed.

## Encode settings

```
-c:v libx264 -crf 17 -preset medium -pix_fmt yuv420p -fps_mode cfr
-c:a aac -b:a 192k -movflags +faststart
```

CRF 17 and yuv420p are fixed to match the existing delivery spec.

## Layout

```
app/            main.py db.py probe.py ffmpeg_ops.py qa.py worker.py static/
inbox/          uploaded source files
work/           in-progress encodes and thumbs/ (failed QA files stay here)
outbox/         delivered files, named {stem}__{speed}x.mp4
logs/           one {job_id}.log per job: full command line + complete ffmpeg stderr
manifests/      manifest.csv, one appended row per completed job
speedlab.db     SQLite - inspectable with any sqlite browser
scripts/        selftest.py
```

When something sounds wrong, `logs/{job_id}.log` has the exact command and the complete
stderr. That is the diagnostic path.

## Self-test

```
.venv\Scripts\python scripts\selftest.py
```

Generates synthetic 1080×1920 clips, boots a real server on port 5071 against a scratch
root (`selftest_run/`, safe to delete), and drives the HTTP API. Ten checks including
timing, the QA gate, duplicate protection, no-audio passthrough, the atempo chain, and a
measured pitch check. Leaves your real `inbox/`, `work/` and `outbox/` untouched.

## Environment variables

| Variable | Effect |
|---|---|
| `SPEEDLAB_ROOT` | Use a different project root for db/inbox/work/outbox/logs |
| `SPEEDLAB_FORCE_ATEMPO=1` | Pretend rubberband is absent (exercises the fallback) |
| `SPEEDLAB_FFMPEG` / `SPEEDLAB_FFPROBE` | Point at specific binaries |
| `SPEEDLAB_AUTH_TOKEN` | Require this token on every request. Unset = open |
| `SPEEDLAB_PUBLIC` | `1` when reachable from outside this machine. Disables the host-command endpoint and applies stricter input defaults |
| `SPEEDLAB_MAX_UPLOAD_MB` / `SPEEDLAB_MAX_FILES` | Per-file size cap, files per request |
| `SPEEDLAB_MAX_TOTAL_DISK_MB` | Stop accepting uploads past this total |
| `SPEEDLAB_MAX_INPUT_SECONDS` / `SPEEDLAB_MAX_INPUT_PIXELS` | Refuse sources longer or larger than this |

## API

```
GET   /                        index.html
GET   /api/capabilities        { rubberband, ffmpeg_version, engine, ... }
POST  /api/upload              multipart, many files -> batch + probed jobs
GET   /api/batches/{id}        full batch + jobs state
PATCH /api/batches/{id}        { match_loudness }
PATCH /api/jobs/{id}           { speed | audio_profile | fps_mode | force | retry }
POST  /api/batches/{id}/start  enqueue all queued jobs
POST  /api/batches/{id}/cancel stop after the current job
GET   /api/thumb/{job_id}      jpeg
GET   /api/output/{job_id}     the finished file
POST  /api/reveal/{job_id}     open the containing folder
GET   /api/log/{job_id}        the job's ffmpeg log
```

## Docker

The image ships Debian's ffmpeg, which **is** built with rubberband — so the container gets
the preferred audio engine with no source build. This is the easiest way to get a correct
environment on any machine.

```
docker compose up -d --build
```

Serves on `http://127.0.0.1:5070`. All state (db, inbox, work, outbox, logs, manifests) lives
in `./data` on the host through a bind mount, so it survives rebuilds and your finished files
land in `data/outbox/`.

## Remote access

SpeedLab defaults to a local, single-user tool and binds `127.0.0.1`. Three things must be
true before it is reachable from anywhere else, and all three are implemented:

**1. Authentication.** Set `SPEEDLAB_AUTH_TOKEN` and every request — pages, API and static
assets alike — needs it. HTTP Basic (any username, the token as password) or
`Authorization: Bearer <token>`. Comparison is constant-time. Unset, the app stays open for
frictionless local use.

**2. Host commands refuse remote callers.** `POST /api/reveal/{id}` runs `open` / `explorer`
on the machine hosting SpeedLab, so it must never be reachable from outside.

A plain "is the client 127.0.0.1?" check is **not enough**, and this was verified rather than
assumed. A tunnel or reverse proxy opens its own connection to the app, so on a natively-run
instance the socket's peer address *is* `127.0.0.1` even for a visitor from the public
internet — the guard passes and the endpoint is exposed. Three layers now apply:

- the socket must be loopback, **and**
- the request must carry no forwarding headers (`X-Forwarded-For`, `CF-Connecting-IP`,
  `CF-Ray`, `Forwarded`, `X-Real-IP`, ngrok and nginx markers), **and**
- `SPEEDLAB_PUBLIC` must not be set.

**Set `SPEEDLAB_PUBLIC=1` whenever this instance is reachable from anywhere but the machine
it runs on.** It disables the endpoint outright and is the only layer that also stops a bare
TCP forwarder, which adds no headers to give itself away. The UI hides the reveal action when
`reveal_available` comes back false.

**3. Per-visitor ownership.** Every visitor is issued an opaque session cookie on first
request. It is **not a login** — no username, no password, nothing to type — but batches and
jobs are scoped to the session that created them. Every read and write
(`/api/batches`, `/api/jobs`, `/api/output`, `/api/log`, `/api/thumb`) authorises against it
and returns `404` — not `403` — on a mismatch, so an id is never confirmed to exist.

This exists because id secrecy alone was not enough: `GET /api/batches/{id}` used to echo the
process-global `current_job`, so anyone could upload a junk file, poll their *own* batch, and
be handed the id of whatever someone else was processing — then pull that job's thumbnail,
log and finished video, pivot to the whole batch, and delete the source. Ids are 48 bits and
unguessable, but they only had to leak once.

**4. Input admission control.** A few-KB file can declare an enormous duration or frame size
and turn one request into hours of encoding, so sources are refused above
`SPEEDLAB_MAX_INPUT_SECONDS` and `SPEEDLAB_MAX_INPUT_PIXELS`, and uploads stop once
`SPEEDLAB_MAX_TOTAL_DISK_MB` is reached. **With `SPEEDLAB_PUBLIC=1` these default to 30
minutes, 4096x4096 and 20 GB**; locally they are unlimited unless you set them.

**5. Upload limits.** `SPEEDLAB_MAX_UPLOAD_MB` (default 4096) is enforced while streaming to
disk, so an oversized POST is aborted mid-write and the partial file removed rather than
filling the disk. `SPEEDLAB_MAX_FILES` (default 50) caps files per request. The file type is
checked *before* any bytes are written, and a file rejected on type, size or probe failure is
removed rather than orphaned in `inbox/`.

Job and batch ids are validated as 12 hex characters before they ever reach a filesystem
path, since `/api/log/{id}` and `/api/thumb/{id}` build paths from them — unvalidated, that is
a traversal primitive, and a UNC path on Windows.

### Exposing it

Put the token in `.env` (gitignored), bring the container up, then tunnel:

```
docker compose up -d
cloudflared tunnel --url http://127.0.0.1:5070
```

Set `SPEEDLAB_PUBLIC=1` in `.env` before doing this. Leaving `SPEEDLAB_AUTH_TOKEN` empty
makes the instance **completely open** — anyone with the URL can upload, process, and
download everything in the outbox. That is a deliberate choice, not an oversight; set the
token if you want it gated.

That prints a `https://<random>.trycloudflare.com` URL. Two caveats: the tunnel is
**ephemeral** — it dies with the process and the URL changes each time — and it only works
while your machine is awake and the container is running. For a stable address you need a
Cloudflare account and a named tunnel against a domain you own.

Even with all of the above, remember what this is: a URL that lets whoever holds the password
run video encodes on your machine. Rotate the token by editing `.env` and running
`docker compose up -d` again.

## Troubleshooting

**Orange banner won't go away** — the FFmpeg first on your PATH has no rubberband. See
above. `where ffmpeg` tells you which one is winning.

**A row failed QA** — open its `log` link. The file is still in `work/`. QA never deletes it.

**Port 5070 in use** — change the port in `run.bat`; nothing else hardcodes it.

**A file processed at the wrong speed** — check `manifests/manifest.csv`, which records the
speed, engine, profile and target fps actually used for every delivered file.
