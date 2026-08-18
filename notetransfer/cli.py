"""notetransfer CLI: turn a video link into note-ready material.

    python -m notetransfer.cli <url-or-share-text> [options]

Produces a workdir containing manifest.json, transcript(s) and frames/.
The manifest is the contract consumed by the note-writing step (an AI agent
or your own template renderer).

Exit codes:
    0  material ready (possibly with warnings)
    2  refused — quality gate failed, no note should be written
    3  duplicate — this URL was processed before (use --force to redo)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from .acquire import acquire
from .common import (PipelineError, RunLog, extract_urls, human_seconds,
                     single_run_lock, url_slug, write_json)
from .frames import extract_frames
from .transcribe import DEFAULT_GLOSSARY, transcribe

LEDGER = os.path.expanduser("~/.notetransfer/ledger.json")


def load_ledger() -> dict:
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def record_ledger(url: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    data = load_ledger()
    data[url] = entry
    write_json(LEDGER, data)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notetransfer",
        description="Video link -> transcript + aligned keyframes")
    p.add_argument("target", help="URL, or raw share text containing a URL")
    p.add_argument("--out", help="work directory (default: temp dir)")
    p.add_argument("--model", default="small",
                   choices=["tiny", "base", "small", "medium", "large-v3"])
    p.add_argument("--max-frames", type=int, default=12)
    p.add_argument("--cookies", help="Netscape cookie file for yt-dlp")
    p.add_argument("--local-file", help="skip download, use this video file")
    p.add_argument("--keep-video", action="store_true",
                   help="keep the downloaded video (default: delete)")
    p.add_argument("--max-duration", type=int, default=3600,
                   help="refuse videos longer than N seconds (default 3600)")
    p.add_argument("--glossary", default=DEFAULT_GLOSSARY,
                   help="domain terms to prime the transcriber")
    p.add_argument("--language", help="force transcript language, e.g. zh, en")
    p.add_argument("--allow-empty-transcript", action="store_true",
                   help="proceed even if no speech was recognised")
    p.add_argument("--force", action="store_true",
                   help="reprocess even if the URL is in the ledger")
    p.add_argument("--no-lock", action="store_true",
                   help="allow concurrent runs (not recommended)")
    p.add_argument("--timeout", type=int, default=90,
                   help="per-page browser timeout in seconds")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = RunLog()

    urls = extract_urls(args.target)
    url = urls[0] if urls else args.target.strip()
    if not url.startswith("http") and not args.local_file:
        print(f"[fatal] no URL found in input: {args.target[:80]}",
              file=sys.stderr)
        return 2
    if urls and len(urls) > 1:
        log.warn(f"{len(urls)} URLs found; using the first one")

    ledger = load_ledger()
    if url in ledger and not args.force:
        prev = ledger[url]
        print(json.dumps({"status": "duplicate", "url": url,
                          "previous": prev}, ensure_ascii=False, indent=2))
        return 3

    out = args.out or tempfile.mkdtemp(prefix="notetransfer_")
    os.makedirs(out, exist_ok=True)
    namespace = url_slug(url)

    try:
        with single_run_lock(enabled=not args.no_lock):
            video, meta = acquire(url, out, log, cookies=args.cookies,
                                  local_file=args.local_file,
                                  timeout=args.timeout)

            if meta.duration and meta.duration > args.max_duration:
                raise PipelineError(
                    f"video is {human_seconds(meta.duration)} long, over the "
                    f"{human_seconds(args.max_duration)} limit; raise "
                    f"--max-duration to proceed")

            with log.stage("transcribe"):
                tr = transcribe(video, out, log, model_size=args.model,
                                glossary=args.glossary, language=args.language)

            with log.stage("frames"):
                frames, strategy = extract_frames(
                    video, out, log, namespace=namespace,
                    max_frames=args.max_frames, segments=tr.segments)

            # Quality gate (audit item A2): refuse to hand over material that
            # would yield a hollow note. Silent "successful failures" — a
            # soundless stream, an empty transcript — are the worst outcome
            # because the note still looks plausible.
            if tr.empty and not args.allow_empty_transcript:
                raise PipelineError(
                    "empty transcript: no speech recognised. The download may "
                    "have captured a video-only stream. Re-run with "
                    "--allow-empty-transcript to build an image-only note.")
            if not frames and tr.empty:
                raise PipelineError("neither transcript nor frames produced")

            if not args.keep_video and os.path.exists(video):
                os.remove(video)
                log.info("source video deleted (pass --keep-video to retain)")

            manifest = {
                "status": "ok",
                "url": url,
                "namespace": namespace,
                "meta": meta.as_dict(),
                "transcript": {
                    "path": tr.path,
                    "raw_path": tr.raw_path,
                    "language": tr.language,
                    "segments": len(tr.segments),
                },
                "frames": [f.as_dict() for f in frames],
                "frame_strategy": strategy,
                "workdir": out,
                "video_kept": args.keep_video,
                "run": log.as_dict(),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            mpath = os.path.join(out, "manifest.json")
            write_json(mpath, manifest)
            record_ledger(url, {"workdir": out, "title": meta.title,
                                "at": manifest["generated_at"]})

            log.info(f"manifest: {mpath}")
            if log.warnings:
                log.info(f"{len(log.warnings)} warning(s) recorded in manifest")
            print(json.dumps({"status": "ok", "manifest": mpath,
                              "title": meta.title,
                              "frames": len(frames),
                              "segments": len(tr.segments),
                              "warnings": log.warnings},
                             ensure_ascii=False, indent=2))
            return 0

    except PipelineError as exc:
        payload = {"status": "refused", "reason": str(exc),
                   "url": url, "run": log.as_dict()}
        write_json(os.path.join(out, "manifest.json"), payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report, never silently pass
        payload = {"status": "error",
                   "reason": f"{type(exc).__name__}: {exc}",
                   "url": url, "run": log.as_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
