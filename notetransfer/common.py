"""Shared utilities: structured logging, warning collection, run locking."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

FFMPEG = os.environ.get("NT_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("NT_FFPROBE", "ffprobe")

LOCK_PATH = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "notetransfer.lock")


class PipelineError(RuntimeError):
    """Fatal error that must stop the pipeline (no note should be produced)."""


@dataclass
class RunLog:
    """Collects stage timings and warnings so failures are observable.

    Rationale (audit item C2): `print` + swallowed exceptions made
    "successful-looking failures" invisible. Every warning recorded here
    is surfaced in manifest.json so the note-writing step can refuse to
    proceed or disclose caveats honestly.
    """

    stages: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _t0: float = field(default_factory=time.time)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[warn] {msg}", file=sys.stderr, flush=True)

    def info(self, msg: str) -> None:
        print(f"[info] {msg}", flush=True)

    @contextmanager
    def stage(self, name: str):
        start = time.time()
        self.info(f"→ {name}")
        try:
            yield
        except Exception as exc:
            self.stages.append({
                "name": name,
                "seconds": round(time.time() - start, 1),
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            raise
        else:
            self.stages.append({
                "name": name,
                "seconds": round(time.time() - start, 1),
                "ok": True,
            })

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": self.stages,
            "warnings": self.warnings,
            "total_seconds": round(time.time() - self._t0, 1),
        }


@contextmanager
def single_run_lock(enabled: bool = True):
    """Serialize pipeline runs (audit item A4).

    Whisper transcription is CPU-bound and synchronous; two concurrent runs
    make the machine unusable. Fail fast instead of thrashing.
    """
    if not enabled:
        yield
        return
    fh = open(LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineError(
                "another notetransfer run is in progress; "
                "wait for it to finish or pass --no-lock")
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def run(cmd: list[str], check: bool = True, text: bool = True):
    return subprocess.run(cmd, check=check, capture_output=True, text=text)


def probe_duration(path: str) -> float:
    """Return media duration in seconds, 0.0 when unknown."""
    try:
        out = run([FFPROBE, "-v", "quiet", "-show_entries",
                   "format=duration", "-of", "csv=p=0", path]).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def probe_stream_kinds(path: str) -> tuple[bool, bool]:
    """Return (has_video, has_audio)."""
    try:
        out = run([FFPROBE, "-v", "error", "-show_entries",
                   "stream=codec_type", "-of", "csv=p=0", path],
                  check=False).stdout
        kinds = {line.strip() for line in out.split()}
        return "video" in kinds, "audio" in kinds
    except Exception:
        return False, False


def file_md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def url_slug(url: str) -> str:
    """Short stable id for a URL, used to namespace frame filenames.

    Rationale (audit item C3): frames were named `wb_frame_01.jpg`, so
    processing a second video silently overwrote the first video's images
    inside the vault. Namespacing by URL hash removes that data-loss risk.
    """
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def extract_urls(text: str) -> list[str]:
    """Pull shareable URLs out of messy share text (Douyin/XHS tokens)."""
    return re.findall(r"https?://[^\s\u4e00-\u9fff,，、）)】\"']+", text)


def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def human_seconds(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
