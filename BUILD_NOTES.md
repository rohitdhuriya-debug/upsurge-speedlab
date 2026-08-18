# BUILD_NOTES

Judgement calls made during the build. Everything the spec decided was followed as written;
this is only the residue.

## Environment differences from the brief

**1. Built on macOS, not `F:\UPSURGE_SpeedLab\`.**
The session opened at `/Users/kunal/Documents/Upsurge_Speed_Videos` on macOS, not the
Windows path in the brief. Rather than hardcode either, every path derives from the project
root (`os.path.dirname(__file__)/..`), so the tree drops onto `F:\UPSURGE_SpeedLab\`
unchanged. `run.bat` is written as specified; `run.command` is the macOS/Linux equivalent
and is what was actually exercised here.

**2. Python is 3.9.6 here, not 3.12.**
The code therefore avoids 3.10+ syntax (no `X | None` unions, no `match`). It runs fine on
3.12; it just doesn't require it.

**3. This machine's FFmpeg (Homebrew 8.1.2) has no rubberband.**
So the *live, executed* engine during every test was the **atempo fallback** — which is the
harder path and the one the brief was most worried about. The rubberband command strings are
built and asserted against the spec character-for-character (self-test 6), but could not be
executed here. On your Windows box with the gyan.dev full build, the banner disappears and
rubberband is used automatically. No code change needed.

**4. A `.venv` was created** because system Python had no FastAPI. `run.bat` and
`run.command` prefer `.venv` if present and fall back to plain `python`.

## Schema additions

The `jobs` table gained two columns beyond the brief's DDL:

- **`force INTEGER DEFAULT 0`** — the brief requires a per-row "force anyway" override on
  duplicates but the schema had nowhere to record that it was used. Without it the manifest
  can't tell a normal delivery from a knowingly-forced one.
- **`fps_mode TEXT DEFAULT 'auto'`** — the fps dropdown has three states (Auto / 30 / 60) but
  `target_fps` only stores the resolved integer. Without `fps_mode`, a row set to "Auto"
  that resolved to 60 is indistinguishable from a row explicitly pinned to 60, so changing
  the speed afterwards wouldn't know whether to recompute. Rows on `auto` recompute their
  target fps whenever the speed changes; pinned rows don't.

## Behaviour decisions

**5. Default speed is 1.25×.** The brief specifies the dropdown contents but not a default.
1.25 is the most conservative option in the list, so nothing gets aggressively sped up by an
accidental Process All. The master bar and every new row start there.

**6. Output filenames trim trailing zeros:** `clip__1.5x.mp4`, and `clip__2x.mp4` rather than
`clip__2.0x.mp4`. Same rule is used inside filter strings (`atempo=1.5`, `tempo=2`).

**7. `target_fps` is stored as an integer.** For a 29.97 fps source at a whole-number speed,
the target rounds to 30 and the `fps` filter locks the output to exactly 30. This is a
deliberate normalisation, not a rounding bug — QA check 5 then compares output fps against
that integer.

**8. Cancel leaves the remaining jobs `queued`** and puts the batch back to `pending`, rather
than inventing a `cancelled` status that isn't in the brief's enum. Effect: pressing Process
All again resumes exactly where it stopped.

**9. Master controls only write to editable rows** (`queued`, `failed`, `skipped_duplicate`).
They never touch a running or already-delivered row.

**10. An unsupported file extension marks that one row failed** rather than rejecting the
whole upload. Dropping 12 files where one is a `.txt` should still process the other 11.

**11. `POST /api/upload` accepts an optional `batch_id`** so a second drag-drop appends to the
batch on screen instead of orphaning the first one. New batch if omitted.

**12. `record_hash` refuses to downgrade an `output` hash to `input`.** Re-uploading a
delivered file writes an input-hash row on ingest; without this guard the `INSERT OR REPLACE`
would overwrite the `role='output'` record and silently disarm duplicate protection for that
file from then on.

## Endpoints beyond the listed surface

Four additions, all needed to satisfy stated requirements:

- **`POST /api/reveal/{job_id}`** — the brief asks for an output link that "opens the file
  location". A browser can't open a folder, so the click posts here and the server calls
  `explorer /select,` (Windows), `open -R` (macOS) or `xdg-open` (Linux).
- **`GET /api/output/{job_id}`** — opens/plays the finished file directly.
- **`GET /api/log/{job_id}`** — surfaces the per-job ffmpeg log from a failed row, since that
  log is described as the only diagnostic path.
- **`PATCH /api/batches/{id}`** — toggles `match_loudness` after upload; otherwise the
  checkbox could only ever be set at ingest time.

`PATCH /api/jobs/{id}` also accepts `retry` to requeue a failed row.

## Implementation details worth knowing

**13. Progress** is parsed from ffmpeg's `time=` against the *expected output* duration
(`src/speed`), clamped to 0.999 until QA finishes. Stages report as
`stretching` → `encoding` → `verifying`. Stretch and encode are one ffmpeg invocation (a
single filter_complex pass), so `stretching` covers process startup and `encoding` begins at
the first progress line.

**14. QA frame counting** prefers the container's `nb_frames`, falls back to
`-count_packets`, and finally to `duration × rate`. The source used is reported in the QA
panel so an estimated count is never mistaken for a measured one.

**15. Loudness correction re-encodes the audio a second time** (`-c:v copy` + `volume=NdB` +
AAC). That is a second AAC generation, which is why it is opt-in and gated at 0.5 LU rather
than always on. Measured drift from a 1.5× stretch was 0.09 LU, so the default of doing
nothing is the right one.

**16. SQLite runs in WAL mode** with a new connection per operation and a lock around
writes — the request thread and the worker thread both write, and a single shared connection
across threads is not safe.

**17. Two env vars exist for testing:** `SPEEDLAB_ROOT` (redirect the whole tree, used so the
self-test never touches your real `outbox/`) and `SPEEDLAB_FORCE_ATEMPO=1` (pretend
rubberband is missing).

## Port: 5070, not 5060

**The brief specified port 5060 and that port does not work in any browser.** 5060 is the SIP
port and sits on the hard-coded blocked-port list in Chrome, Firefox and Safari. The server
binds it fine and `curl` gets a clean 200, but a browser refuses to connect at all and shows
`ERR_UNSAFE_PORT`. 5061 (SIP over TLS) is blocked for the same reason, so the self-test moved
off it too.

The default is now **5070** — still clear of the 5050 that another app of yours occupies, and
not on any browser's block list. Self-test uses 5071.

If you ever need 5060 specifically, the only way is to launch the browser with
`--explicitly-allowed-ports=5060`, which has to be repeated on every launch. Not worth it;
change the port in `run.bat` instead.

## Later additions

**21. FFmpeg binary auto-discovery (`app/bins.py`).** The first `ffmpeg` on PATH is not always
the best one installed — a machine can carry a stock build on PATH and a rubberband-capable
build alongside it, and the app would have silently fallen back to atempo. It now scans PATH,
then Homebrew prefixes and Cellar on macOS, then `C:\ffmpeg\bin`, `C:\Program Files\ffmpeg\bin`
and any `C:\ffmpeg*\bin` on Windows, and uses the first build that actually reports the
rubberband filter. `ffprobe` is always paired from the same build so the two can never come
from different versions. `SPEEDLAB_FFMPEG` overrides the scan entirely and is never probed
against. The chosen path and the reason are exposed at `/api/capabilities` and on hover over
the header; `POST /api/capabilities/rescan` re-runs it without a restart.

Practical effect on Windows: extract the gyan.dev full build to `C:\ffmpeg` and restart —
no PATH surgery, and no fighting whichever ffmpeg already wins there.

**22. rubberband was installed on this machine.** Homebrew's core ffmpeg is not built with it,
and the `homebrew-ffmpeg` tap uses the same formula name, so Homebrew refuses to hold both —
the swap required `brew uninstall ffmpeg` first. Checked `brew uses --installed ffmpeg` beforehand:
nothing depended on it. Result: **ffmpeg 8.1.2 → 9.0.1 with rubberband**, built from source in
8 minutes. Revert with `brew uninstall ffmpeg && brew install ffmpeg`.

**23. Delete semantics are deliberately asymmetric.** `DELETE /api/jobs/{id}` removes the row,
the uploaded copy in `inbox/`, the thumbnail and any working file — but never a delivered file
in `outbox/`. Deleting someone's finished output because they tidied a list would be the wrong
default, and it is trivially done in Finder if actually wanted. Two further rules:

- A `running` job returns `409`. Unlinking a file under an open ffmpeg process is asking for a
  corrupt output and a confusing log.
- The job's `input` hash record is cleared so the file can be re-added; `output` hashes are
  never touched, since removing one disarms duplicate protection for that file permanently.

A path guard (`_under`) means a delete can only ever unlink inside `inbox/` and `work/`. This
is tested with a job row deliberately pointing outside the project: the file survives.

**24. `Clear` became `Remove All`** and now actually deletes the batch rather than hiding it
from view. The old behaviour leaked — abandoned uploads accumulated in `inbox/` with no way to
see or remove them from the UI.

**25. White theme.** The brief asked for a dark work-tool palette and that is what was built
first; the light theme was a later request. It is a light design rather than an inverted dark
one — status colours were retuned for contrast on white, and the amber warning badge and red
duplicate rows became tinted backgrounds with matching borders instead of dark blocks. Density
and monospaced numerals were kept; the only motion added is hover/focus transitions and an
eased progress bar.

## Testing notes

**18. The self-test runs 10 checks, not 6.** The six required, plus:

- **7** — the Definition of Done requires pitch be *measured*, not assumed. A 220 Hz sine is
  encoded into the source and the output's fundamental is recovered by Goertzel analysis over
  a decoded window. Result: 220 Hz in, 220 Hz out, where a naive resample would read 330 Hz.
- **8** — the Definition of Done scenario itself: a batch with master speed applied to all
  rows and two rows overridden afterwards, verifying the overrides survive and each file
  lands with the right duration and filename.
- **9** — the loudness path: measurement on both sides, plus a direct check that a known
  +3 dB gain moves measured LUFS by 3.00.
- **10** — a negative test proving the QA gate actually bites: correct file with a wrong
  claimed speed, wrong claimed fps, an unparseable file and a missing file are all rejected,
  while the genuine article still passes. Without this, five checks that always return true
  would look identical to five working ones.

**19. The frontend was verified headlessly.** The browser pane in this session could not
reach `localhost`, so `app.js` was executed against a minimal DOM shim with real API payloads
to confirm row rendering, the amber >1.5× reel badge and its tooltip, the red duplicate row
and its Force button, select locking on running/done rows, the `Auto (60)` / `Auto (30)`
computed labels, QA panel expansion, and that master controls patch only editable rows. That
harness was scratch tooling and is not part of the delivered tree — worth re-checking the
page visually on your machine, since pixels were never rendered here.

**20. The self-test binds port 5071**, not 5070, so it can run while the app is up.
It writes everything into `selftest_run/`, which is safe to delete.
