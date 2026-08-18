"""Locate the FFmpeg binaries, preferring a build that actually has rubberband.

The first ffmpeg on PATH is often not the best one available - a machine can easily
carry a stock build on PATH and a rubberband-capable build alongside it. Rather than
silently falling back to atempo in that situation, scan the usual locations and pick
the capable one. An explicit SPEEDLAB_FFMPEG always wins and is never second-guessed.
"""
import glob
import os
import platform
import shutil
import subprocess

_PROBE_TIMEOUT = 30


def _has_rubberband(path):
    """True / False, or None when the binary can't be run at all."""
    try:
        proc = subprocess.run([path, "-hide_banner", "-filters"],
                              capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        # Rows look like: " T.. rubberband  A->A  Apply time-stretching..."
        if len(parts) >= 2 and parts[1] == "rubberband":
            return True
    return False


def _candidates():
    found, ordered = set(), []

    def add(path):
        if path and path not in found and os.path.exists(path):
            found.add(path)
            ordered.append(path)

    add(shutil.which("ffmpeg"))

    system = platform.system()
    if system == "Darwin":
        add("/opt/homebrew/bin/ffmpeg")
        add("/usr/local/bin/ffmpeg")
        for cellar in ("/opt/homebrew/Cellar/ffmpeg/*/bin/ffmpeg",
                       "/usr/local/Cellar/ffmpeg/*/bin/ffmpeg"):
            for path in sorted(glob.glob(cellar), reverse=True):
                add(path)
    elif system == "Windows":
        for path in (r"C:\ffmpeg\bin\ffmpeg.exe",
                     r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                     r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe"):
            add(path)
        # gyan.dev archives usually extract as C:\ffmpeg-<version>-full_build\
        for path in sorted(glob.glob(r"C:\ffmpeg*\bin\ffmpeg.exe"), reverse=True):
            add(path)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for path in sorted(glob.glob(os.path.join(local, "ffmpeg*", "bin", "ffmpeg.exe")),
                               reverse=True):
                add(path)
    else:
        add("/usr/bin/ffmpeg")
        add("/usr/local/bin/ffmpeg")

    return ordered


def _sibling_ffprobe(ffmpeg_path):
    """Pair ffprobe with the chosen ffmpeg so the two never come from different builds."""
    directory = os.path.dirname(ffmpeg_path)
    if directory:
        name = "ffprobe.exe" if ffmpeg_path.lower().endswith(".exe") else "ffprobe"
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
    return shutil.which("ffprobe") or "ffprobe"


def resolve():
    forced = os.environ.get("SPEEDLAB_FFMPEG")
    if forced:
        return {
            "ffmpeg": forced,
            "ffprobe": os.environ.get("SPEEDLAB_FFPROBE") or _sibling_ffprobe(forced),
            "rubberband": _has_rubberband(forced) is True,
            "source": "SPEEDLAB_FFMPEG",
            "scanned": [forced],
        }

    scanned = _candidates()
    first_usable = None
    for path in scanned:
        state = _has_rubberband(path)
        if state is True:
            return {
                "ffmpeg": path,
                "ffprobe": os.environ.get("SPEEDLAB_FFPROBE") or _sibling_ffprobe(path),
                "rubberband": True,
                "source": "auto-detected (rubberband)",
                "scanned": scanned,
            }
        if state is False and first_usable is None:
            first_usable = path

    chosen = first_usable or (scanned[0] if scanned else "ffmpeg")
    return {
        "ffmpeg": chosen,
        "ffprobe": os.environ.get("SPEEDLAB_FFPROBE") or _sibling_ffprobe(chosen),
        "rubberband": False,
        "source": "PATH" if scanned else "not found",
        "scanned": scanned,
    }


FFMPEG = FFPROBE = SOURCE = ""
RUBBERBAND = False
SCANNED = []


def reload():
    """Re-scan. Lets a freshly installed ffmpeg be picked up without a restart."""
    global FFMPEG, FFPROBE, RUBBERBAND, SOURCE, SCANNED
    found = resolve()
    FFMPEG = found["ffmpeg"]
    FFPROBE = found["ffprobe"]
    RUBBERBAND = found["rubberband"]
    SOURCE = found["source"]
    SCANNED = found["scanned"]
    return found


reload()
