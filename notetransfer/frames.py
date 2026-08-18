"""Keyframe extraction: scene detection, perceptual dedup, exact re-seek.

Design notes tied to audit findings:

  * B4 — timestamps used to be paired with filenames by zipping ffmpeg's
    `showinfo` log against a sorted directory listing. Any extra log line or
    dropped frame shifted the whole mapping, and the code silently fell back
    to `-1.0`. Now every kept frame is re-extracted with `-ss <exact time>`,
    so filename and timestamp cannot drift apart.
  * C3 — frames were named `wb_frame_01.jpg`, so a second video overwrote the
    first one's images in the vault. Names now carry a per-URL namespace.
  * C4 — the three-tier strategy reports which tier actually fired, so docs
    can state real hit rates instead of assuming scene detection worked.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .common import FFMPEG, RunLog, probe_duration, run

SCENE_THRESHOLDS = (0.25, 0.12)
HAMMING_MIN = 6


@dataclass
class Frame:
    path: str
    timestamp: float
    context: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "timestamp": self.timestamp,
                "context": self.context}


def _dhash(gray: bytes) -> int:
    """64-bit difference hash from a 9x8 grayscale buffer."""
    h = 0
    for y in range(8):
        row = gray[y * 9:(y + 1) * 9]
        if len(row) < 9:
            return h
        for x in range(8):
            h = (h << 1) | (1 if row[x] > row[x + 1] else 0)
    return h


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _hash_at(video: str, t: float) -> int | None:
    proc = subprocess.run(
        [FFMPEG, "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
         "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"],
        capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < 72:
        return None
    return _dhash(proc.stdout)


def _scene_times(video: str, threshold: float, cap: int,
                 log: RunLog) -> list[float]:
    proc = subprocess.run(
        [FFMPEG, "-i", video, "-vf",
         f"select='gt(scene,{threshold})',showinfo",
         "-vsync", "vfr", "-f", "null", "-"],
        capture_output=True, text=True)
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", proc.stderr)]
    log.info(f"  scene>{threshold}: {len(times)} cuts")
    return times[:cap]


def _interval_times(video: str, count: int) -> list[float]:
    dur = probe_duration(video) or 600.0
    step = dur / (count + 1)
    return [round(step * (i + 1), 2) for i in range(count)]


def select_timestamps(video: str, max_frames: int,
                      log: RunLog) -> tuple[list[float], str]:
    """Pick candidate timestamps; returns (times, strategy_used)."""
    for th in SCENE_THRESHOLDS:
        times = _scene_times(video, th, max_frames * 4, log)
        if len(times) >= max(3, max_frames // 3):
            return times, f"scene>{th}"
    log.info("  scene detection insufficient → interval sampling")
    return _interval_times(video, max_frames * 2), "interval"


def extract_frames(video: str, out: str, log: RunLog, namespace: str,
                   max_frames: int = 12,
                   segments: list[dict] | None = None) -> tuple[list[Frame], str]:
    from .transcribe import context_around

    fdir = os.path.join(out, "frames")
    os.makedirs(fdir, exist_ok=True)

    times, strategy = select_timestamps(video, max_frames, log)
    if not times:
        log.warn("no candidate frames found")
        return [], strategy

    kept: list[float] = []
    hashes: list[int] = []
    for t in times:
        h = _hash_at(video, t)
        if h is None:
            continue
        if all(_hamming(h, kh) > HAMMING_MIN for kh in hashes):
            kept.append(t)
            hashes.append(h)

    if len(kept) > max_frames:
        step = len(kept) / max_frames
        kept = [kept[int(i * step)] for i in range(max_frames)]

    frames: list[Frame] = []
    for idx, t in enumerate(kept, 1):
        name = f"{namespace}_{idx:02d}_t{int(t)}s.jpg"
        path = os.path.join(fdir, name)
        proc = subprocess.run(
            [FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
             "-vf", "scale=1280:-2", "-q:v", "3", path],
            capture_output=True)
        if proc.returncode != 0 or not os.path.exists(path):
            log.warn(f"frame extraction failed at {t:.1f}s")
            continue
        frames.append(Frame(path=path, timestamp=t,
                            context=context_around(segments or [], t)))

    log.info(f"{len(frames)} frames kept via {strategy} (+dedup)")
    if not frames:
        log.warn("no frames extracted — note will be text only")
    return frames, strategy
