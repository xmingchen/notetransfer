"""Acquisition layer: get a playable local video + metadata from a URL.

Three tiers, tried in order (audit item A1 — the old design had no third tier,
so a single upstream change broke the only working path):

  1. yt-dlp            — 1800+ sites, cheapest and most maintained
  2. browser sniffing  — real Chromium, read media URLs off the network
  3. manual handoff    — caller supplies an already-downloaded file

Tier 3 is not a nicety: platforms actively fight scrapers, and a pipeline
whose last line of defence is our own regex will eventually fail.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Optional

from .common import (FFMPEG, PipelineError, RunLog, probe_duration,
                     probe_stream_kinds, run)

CDN_HINTS = ("douyinvod", "xhscdn", "sns-video", "bytecdn", "tiktokcdn",
             "akamaized", "googlevideo", "bilivideo")
DECOY_HINTS = ("static", "effect", "sprite", "placeholder", "logo")
MEDIA_HINTS = CDN_HINTS + (".mp4", ".m4a", ".m4s")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")


@dataclass
class Meta:
    title: str = ""
    uploader: str = "unknown"
    description: str = ""
    duration: float = 0.0
    webpage_url: str = ""
    platform: str = ""
    upload_date: str = ""
    source_tier: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def find_chromium() -> Optional[str]:
    """Locate a Chromium binary at runtime (audit item C1).

    The previous implementation hardcoded `chromium-1228`; a Playwright
    upgrade or cache purge silently removed the fallback path entirely.
    """
    env = os.environ.get("NT_CHROMIUM")
    if env and os.path.exists(env):
        return env
    patterns = [
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/"
                           "chrome-mac*/Google Chrome for Testing.app/"
                           "Contents/MacOS/Google Chrome for Testing"),
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]
    hits: list[str] = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    if hits:
        # highest build number wins
        def build_no(p: str) -> int:
            m = re.search(r"chromium-(\d+)", p)
            return int(m.group(1)) if m else 0
        return sorted(hits, key=build_no)[-1]
    for path in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 shutil.which("chromium") or "",
                 shutil.which("google-chrome") or ""):
        if path and os.path.exists(path):
            return path
    return None


# --------------------------------------------------------------------------
# tier 1: yt-dlp
# --------------------------------------------------------------------------

def via_ytdlp(url: str, out: str, log: RunLog,
              cookies: Optional[str] = None) -> tuple[str, Meta]:
    import yt_dlp

    opts = {
        "outtmpl": os.path.join(out, "video.%(ext)s"),
        "format": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
    }
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    video = None
    for fn in sorted(os.listdir(out)):
        if fn.startswith("video.") and not fn.endswith(".json"):
            video = os.path.join(out, fn)
    if not video:
        raise PipelineError("yt-dlp reported success but produced no file")

    meta = Meta(
        title=(info.get("title") or "").strip(),
        uploader=(info.get("uploader") or info.get("channel")
                  or info.get("uploader_id") or "unknown").strip(),
        description=(info.get("description") or "")[:2000],
        duration=float(info.get("duration") or 0) or probe_duration(video),
        webpage_url=info.get("webpage_url") or url,
        platform=(info.get("extractor_key") or "").lower(),
        upload_date=info.get("upload_date") or "",
        source_tier="yt-dlp",
    )
    log.info(f"yt-dlp ok: {meta.title[:60]} ({meta.duration:.0f}s)")
    return video, meta


# --------------------------------------------------------------------------
# tier 2: headless browser sniffing
# --------------------------------------------------------------------------

def _stream_download(url: str, dest: str, referer: str, cookie: str,
                     log: RunLog) -> None:
    """Download to disk in chunks (audit item A3).

    The old code did `f.write(response.body())`, buffering the whole file in
    RAM — fine for a 30 MB clip, fatal for a 1-hour 1080p video.
    """
    cmd = ["curl", "-sL", "--fail", "--max-time", "1800",
           "-A", UA, "-H", f"Referer: {referer}"]
    if cookie:
        cmd += ["-H", f"Cookie: {cookie}"]
    cmd += ["-o", dest, url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dest):
        raise PipelineError(f"stream download failed (curl {proc.returncode})")
    log.info(f"  fetched {os.path.getsize(dest) / 1e6:.1f} MB")


def _score(url: str, size: int) -> int:
    s = size
    if any(h in url for h in CDN_HINTS):
        s += 10 ** 12
    if any(h in url for h in DECOY_HINTS):
        s -= 10 ** 12
    return s


def via_browser(url: str, out: str, log: RunLog,
                timeout: int = 90) -> tuple[str, Meta]:
    from playwright.sync_api import sync_playwright

    chromium = find_chromium()
    if not chromium:
        raise PipelineError(
            "no Chromium found. Install with: python -m playwright install chromium")

    found: dict[str, dict] = {}

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            u = resp.url
            if not u.startswith("http") or u in found:
                return
            if ("video" in ct or "audio" in ct
                    or any(h in u for h in MEDIA_HINTS)):
                found[u] = {
                    "size": int(resp.headers.get("content-length") or 0),
                    "ct": ct,
                }
        except Exception as exc:  # noqa: BLE001 - never break page loading
            log.warn(f"response handler: {type(exc).__name__}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium, headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                  viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as exc:
            log.warn(f"page.goto: {type(exc).__name__} (continuing)")

        for _ in range(max(timeout // 3, 4)):
            try:
                page.evaluate(
                    "document.querySelectorAll('video').forEach(v=>{"
                    "v.muted=true;v.play().catch(()=>{})})")
                page.mouse.wheel(0, 400)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            if len([u for u in found if any(h in u for h in CDN_HINTS)]) >= 2:
                break

        meta = _page_meta(page, url, log)
        cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        referer = page.url
        browser.close()

    if not found:
        raise PipelineError("no media stream observed on the page")

    ranked = sorted(found.items(), key=lambda kv: _score(kv[0], kv[1]["size"]),
                    reverse=True)
    log.info(f"{len(ranked)} media candidates observed")

    # Walk *all* candidates until we hold a video track and an audio track
    # (audit item A2: the old `ranked[:2]` could grab two video streams and
    # silently produce a soundless file, which then produced an empty
    # transcript and a content-free note).
    video_file = audio_file = combined = None
    for cand_url, _info in ranked:
        if combined or (video_file and audio_file):
            break
        tmp = os.path.join(out, f"cand_{len(os.listdir(out))}.bin")
        try:
            _stream_download(cand_url, tmp, referer, cookie, log)
        except PipelineError as exc:
            log.warn(f"candidate skipped: {exc}")
            continue
        has_v, has_a = probe_stream_kinds(tmp)
        if has_v and has_a:
            combined = tmp
        elif has_v and not video_file:
            video_file = tmp
        elif has_a and not audio_file:
            audio_file = tmp
        else:
            os.remove(tmp)

    dest = os.path.join(out, "video.mp4")
    if combined:
        os.replace(combined, dest)
    elif video_file and audio_file:
        run([FFMPEG, "-y", "-i", video_file, "-i", audio_file, "-c", "copy",
             "-map", "0:v:0", "-map", "1:a:0", dest])
        log.info("merged separate video + audio tracks")
    elif video_file:
        os.replace(video_file, dest)
        log.warn("video has no audio track — transcription will be empty")
    else:
        raise PipelineError("no playable video track among candidates")

    for leftover in glob.glob(os.path.join(out, "cand_*.bin")):
        os.remove(leftover)

    meta.duration = probe_duration(dest)
    meta.source_tier = "browser-sniff"
    return dest, meta


def _page_meta(page, url: str, log: RunLog) -> Meta:
    """Read metadata from structured tags, never from title regex.

    Audit item B2: the old code guessed the author with `re.search(r"@(...)")`
    on the page title, which produced wrong or empty `author` fields. An
    incorrect author in the vault is worse than an explicit "unknown".
    """
    def attr(selector: str, name: str = "content") -> str:
        try:
            el = page.query_selector(selector)
            return (el.get_attribute(name) or "").strip() if el else ""
        except Exception:
            return ""

    title = attr("meta[property='og:title']") or (page.title() or "")
    title = re.sub(r"\s*[-|·]\s*(抖音|小红书|哔哩哔哩|YouTube).*$", "", title).strip()
    desc = attr("meta[property='og:description']") or attr("meta[name='description']")
    author = (attr("meta[name='author']")
              or attr("meta[property='og:video:director']")
              or attr("meta[property='article:author']"))

    if not author:
        for script in page.query_selector_all("script[type='application/ld+json']"):
            try:
                blob = json.loads(script.inner_text())
            except Exception:
                continue
            for node in (blob if isinstance(blob, list) else [blob]):
                if not isinstance(node, dict):
                    continue
                cand = node.get("author") or node.get("creator")
                if isinstance(cand, dict):
                    cand = cand.get("name")
                if isinstance(cand, str) and cand.strip():
                    author = cand.strip()
                    break
            if author:
                break

    if not author:
        log.warn("author not found in page metadata; left as 'unknown'")

    host = page.url
    platform = ("douyin" if "douyin" in host else
                "xiaohongshu" if ("xiaohongshu" in host or "xhslink" in host) else
                "bilibili" if "bilibili" in host else "web")
    return Meta(title=title or "未命名视频", uploader=author or "unknown",
                description=desc[:2000], webpage_url=page.url or url,
                platform=platform)


# --------------------------------------------------------------------------
# tier 3: manual handoff
# --------------------------------------------------------------------------

def via_local_file(path: str, out: str, log: RunLog,
                   url: str = "") -> tuple[str, Meta]:
    if not os.path.exists(path):
        raise PipelineError(f"local file not found: {path}")
    dest = os.path.join(out, "video" + os.path.splitext(path)[1].lower())
    shutil.copy2(path, dest)
    has_v, _ = probe_stream_kinds(dest)
    if not has_v:
        raise PipelineError(f"file has no video track: {path}")
    log.info(f"using local file: {os.path.basename(path)}")
    return dest, Meta(
        title=os.path.splitext(os.path.basename(path))[0],
        uploader="unknown",
        duration=probe_duration(dest),
        webpage_url=url,
        platform="local",
        source_tier="local-file",
    )


def acquire(url: str, out: str, log: RunLog, cookies: Optional[str] = None,
            local_file: Optional[str] = None,
            timeout: int = 90) -> tuple[str, Meta]:
    if local_file:
        with log.stage("acquire:local-file"):
            return via_local_file(local_file, out, log, url)
    try:
        with log.stage("acquire:yt-dlp"):
            return via_ytdlp(url, out, log, cookies)
    except Exception as exc:
        log.warn(f"yt-dlp failed ({type(exc).__name__}), trying browser sniff")
    with log.stage("acquire:browser-sniff"):
        return via_browser(url, out, log, timeout)
