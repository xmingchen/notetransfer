"""Transcription: local Whisper with domain-vocabulary priming.

Audit item B3: `small` produced dense homophone errors on technical Chinese
(向量库→"香凉库", 索引→"锁引", 权限→"全线"). Those were fixed by hand during
note writing, with no record of what changed — a silent-corruption risk for
unfamiliar domains. Two mitigations here:

  * `initial_prompt` is primed with a domain glossary, cutting errors at source
  * the raw transcript is always kept, so every correction stays auditable
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .common import PipelineError, RunLog, human_seconds, run, FFMPEG

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# hf-mirror does not proxy the xet CAS backend; leaving it on yields HTTP 401.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

DEFAULT_GLOSSARY = (
    "RAG 检索增强生成 向量库 向量化 embedding 索引 chunk 切块 召回 重排序 rerank "
    "上下文 权限 知识库 大模型 提示词 置信度 拒答 评测集 元数据 语义"
)


@dataclass
class Transcript:
    path: str
    raw_path: str
    segments: list[dict]
    language: str
    empty: bool


def extract_audio(video: str, out: str) -> str:
    wav = os.path.join(out, "audio.wav")
    run([FFMPEG, "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav])
    return wav


def transcribe(video: str, out: str, log: RunLog, model_size: str = "small",
               glossary: str = DEFAULT_GLOSSARY,
               language: str | None = None) -> Transcript:
    from faster_whisper import WhisperModel

    wav = extract_audio(video, out)
    try:
        log.info(f"loading whisper '{model_size}' (first run downloads weights)")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        prompt = (f"以下是一段中文视频内容，请使用简体中文输出。"
                  f"可能出现的专业词汇：{glossary}。")
        segments, info = model.transcribe(
            wav, initial_prompt=prompt, vad_filter=True, language=language,
            condition_on_previous_text=False)

        rows: list[dict] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            rows.append({"start": round(seg.start, 1),
                         "end": round(seg.end, 1),
                         "text": text})
    finally:
        if os.path.exists(wav):
            os.remove(wav)

    raw_path = os.path.join(out, "transcript.raw.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"[{human_seconds(r['start'])}] {r['text']}\n")

    path = os.path.join(out, "transcript.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"[{human_seconds(r['start'])}] {r['text']}"
                          for r in rows))

    empty = len(rows) == 0
    if empty:
        log.warn("transcript is EMPTY — video likely has no speech/audio")
    else:
        log.info(f"transcribed {len(rows)} segments, lang={info.language}")
    return Transcript(path=path, raw_path=raw_path, segments=rows,
                      language=info.language if not empty else "",
                      empty=empty)


def context_around(segments: list[dict], t: float, window: float = 15.0) -> str:
    """Transcript text spoken around timestamp `t` (audit item B1).

    Previously frames and transcript lived on separate timelines and were only
    listed side by side in the manifest, so captioning a frame meant guessing
    which section it belonged to. That happened to work on a slide-deck video
    where sections mapped linearly onto time; on interleaved edits it would
    mis-caption, and a confidently wrong caption is worse than no image.
    """
    if not segments:
        return ""
    lo, hi = t - window, t + window
    hits = [s["text"] for s in segments
            if s["end"] >= lo and s["start"] <= hi]
    if not hits:
        nearest = min(segments, key=lambda s: abs(s["start"] - t))
        hits = [nearest["text"]]
    return " ".join(hits)[:400]
