from __future__ import annotations

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .gemini_client import GeminiJsonClient
from .models import Cue, DemoMoment
from .srt_utils import format_timestamp


# Short chunks keep Gemini's audio timestamps accurate; requests run in parallel.
CHUNK_SECONDS = 300.0
MIN_TAIL_SECONDS = 20.0
MAX_PARALLEL_REQUESTS = 4
MIN_DEMO_CONFIDENCE = 0.6
MIN_DEMO_SECONDS = 0.8
DEMO_MERGE_GAP = 0.6

T = TypeVar("T")


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    offset: float
    duration: float


def parse_clip_time(value: object) -> float | None:
    """Parse seconds, "SS.mmm", "MM:SS.mmm" or "HH:MM:SS.mmm"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    seconds = 0.0
    for part in text.split(":"):
        try:
            seconds = seconds * 60 + float(part)
        except ValueError:
            return None
    return seconds


def _chunk_bounds(duration: float, chunk_seconds: float) -> list[tuple[float, float]]:
    bounds: list[tuple[float, float]] = []
    start = 0.0
    while start < duration - 0.05:
        end = min(duration, start + chunk_seconds)
        if duration - end < MIN_TAIL_SECONDS:
            end = duration
        bounds.append((start, end))
        start = end
    return bounds


def extract_audio_chunks(
    media: Path,
    duration: float,
    out_dir: Path,
    cancel_event: threading.Event,
    chunk_seconds: float = CHUNK_SECONDS,
) -> list[AudioChunk]:
    """Extract small mono MP3 chunks that can be sent inline to Gemini."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[AudioChunk] = []
    for index, (start, end) in enumerate(_chunk_bounds(duration, chunk_seconds), start=1):
        if cancel_event.is_set():
            raise InterruptedError("Stopped by user.")
        path = out_dir / f"audio_chunk_{index:03d}.mp3"
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{end - start:.3f}",
                "-i",
                str(media),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "48k",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0 or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(
                f"Could not extract audio from {media.name}: {result.stderr.strip()}"
            )
        chunks.append(AudioChunk(path=path, offset=start, duration=end - start))
    if not chunks:
        raise RuntimeError(f"{media.name} has no audio to analyze.")
    return chunks


def _run_parallel(
    chunks: Sequence[AudioChunk], job: Callable[[int, AudioChunk], T]
) -> list[T]:
    workers = max(1, min(MAX_PARALLEL_REQUESTS, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(job, index, chunk) for index, chunk in enumerate(chunks, start=1)
        ]
        return [future.result() for future in futures]


def _clip_ranges(
    items: Sequence[dict[str, Any]], chunk: AudioChunk
) -> list[tuple[float, float, dict[str, Any]]]:
    """Validate clip-relative start/end values and convert them to absolute times."""
    parsed: list[tuple[float, float | None, dict[str, Any]]] = []
    for item in items:
        start = parse_clip_time(item.get("start"))
        if start is None:
            continue
        parsed.append((start, parse_clip_time(item.get("end")), item))
    if not parsed:
        return []
    # Some models answer with absolute video time instead of clip time.
    if chunk.offset > 0 and max(start for start, _, _ in parsed) > chunk.duration + 1.0:
        if all(start >= chunk.offset - 1.0 for start, _, _ in parsed):
            parsed = [
                (start - chunk.offset, None if end is None else end - chunk.offset, item)
                for start, end, item in parsed
            ]
    parsed.sort(key=lambda entry: entry[0])
    result: list[tuple[float, float, dict[str, Any]]] = []
    for index, (start, end, item) in enumerate(parsed):
        start = max(0.0, min(chunk.duration, start))
        next_start = parsed[index + 1][0] if index + 1 < len(parsed) else chunk.duration
        if end is None or end <= start:
            end = min(start + 3.0, max(start, next_start))
        end = max(0.0, min(chunk.duration, end))
        if end - start < 0.1:
            continue
        result.append((chunk.offset + start, chunk.offset + end, item))
    return result


def transcribe_media(
    client: GeminiJsonClient,
    media: Path,
    duration: float,
    work_dir: Path,
    logger: Callable[[str], None],
    cancel_event: threading.Event,
) -> list[Cue]:
    """Create subtitle cues for any video/audio file by listening with Gemini."""
    audio_dir = work_dir / f"_audio_{media.stem}"
    try:
        chunks = extract_audio_chunks(media, duration, audio_dir, cancel_event)
        logger(f"Audio split into {len(chunks)} chunk(s) for transcription.")

        def job(index: int, chunk: AudioChunk) -> list[tuple[float, float, str]]:
            prompt = f"""
Transcribe this audio clip into subtitle cues.

Rules:
- Transcribe speech verbatim in the language actually spoken. Do not translate,
  summarize, correct, or censor. Keep product names and numbers exactly.
- Timestamps are relative to the start of THIS clip, format "MM:SS.mmm".
  The clip is {chunk.duration:.1f} seconds long.
- Each cue is one short phrase or sentence: at most about 7 seconds and 90 characters.
- start/end must follow the actual speech closely. No cues for silence or music.
- The clip may begin or end in the middle of a sentence; transcribe what is audible.
- Return JSON only:
{{"cues":[{{"start":"00:01.250","end":"00:03.900","text":"..."}}]}}
""".strip()
            payload = client.generate_json(
                [client.audio_part(chunk.path), prompt],
                f"SRT {index}/{len(chunks)}",
                max_output_tokens=65536,
            )
            raw = payload.get("cues") if isinstance(payload, dict) else payload
            items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            cues = [
                (start, end, " ".join(str(item.get("text", "")).split()))
                for start, end, item in _clip_ranges(items, chunk)
            ]
            return [cue for cue in cues if cue[2]]

        results = _run_parallel(chunks, job)
    finally:
        shutil.rmtree(audio_dir, ignore_errors=True)

    cues: list[Cue] = []
    for chunk_cues in results:
        for start, end, text in chunk_cues:
            if cues and start < cues[-1].end:
                previous = cues[-1]
                if start - previous.start < 0.1:
                    continue
                cues[-1] = Cue(previous.id, previous.start, start, previous.text)
            cues.append(Cue(id=len(cues) + 1, start=start, end=end, text=text))
    if not cues:
        raise RuntimeError(f"Gemini did not find any speech in {media.name}.")
    return cues


def detect_demo_moments(
    client: GeminiJsonClient,
    media: Path,
    duration: float,
    source_cues: Sequence[Cue],
    work_dir: Path,
    logger: Callable[[str], None],
    cancel_event: threading.Event,
) -> list[DemoMoment]:
    """Find spans of the original audio that must stay audible (demo sound)."""
    audio_dir = work_dir / "_demo_audio"
    try:
        chunks = extract_audio_chunks(media, duration, audio_dir, cancel_event)

        def job(index: int, chunk: AudioChunk) -> list[tuple[float, float, dict[str, Any]]]:
            chunk_end = chunk.offset + chunk.duration
            transcript = "\n".join(
                f"[{format_timestamp(max(0.0, cue.start - chunk.offset))[3:]} --> "
                f"{format_timestamp(max(0.0, cue.end - chunk.offset))[3:]}] {cue.text}"
                for cue in source_cues
                if cue.end > chunk.offset and cue.start < chunk_end
            )
            prompt = f"""
You are listening to one clip of the ORIGINAL audio track of a video. The video
will be re-edited with a NEW narration that replaces the original presenter's
voice. Find the moments where the ORIGINAL audio itself is demo content the
viewer must HEAR, because muting it would lose the point of the demo. Examples:
- generated/synthesized voice or TTS samples, voice-cloning results
- music, songs, beats or any sound produced by the product/tool being shown
- app, game or device sound effects that are part of what is demonstrated
- a recording, video or call being played back inside the video
- before/after audio comparisons
- another person speaking as part of the demo (interview, customer, character)

Do NOT include:
- the presenter talking, explaining or narrating, even while showing the screen
- background or intro music that only sits under the presenter's voice
- silence, typing/clicking noise, transitions, intro/outro jingles

Timestamps are relative to the start of THIS clip, format "MM:SS.mmm". The clip
is {chunk.duration:.1f} seconds long. Each moment spans exactly the demo sound,
not the presenter's lead-in sentence. confidence is 0.0 to 1.0.
kind is one of: voice_sample, music, sound_effect, playback, other_speaker, other.
Return JSON only:
{{"moments":[{{"start":"00:12.300","end":"00:19.800","kind":"voice_sample","description":"short description","confidence":0.9}}]}}
Return {{"moments":[]}} when nothing qualifies; that is common.

TRANSCRIPT OF THIS CLIP (may contain presenter speech and demo speech; times relative to the clip):
{transcript or "(no transcript cues in this clip)"}
""".strip()
            payload = client.generate_json(
                [client.audio_part(chunk.path), prompt],
                f"demo detection {index}/{len(chunks)}",
                max_output_tokens=16384,
            )
            raw = payload.get("moments") if isinstance(payload, dict) else payload
            items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            return _clip_ranges(items, chunk)

        results = _run_parallel(chunks, job)
    finally:
        shutil.rmtree(audio_dir, ignore_errors=True)

    spans: list[tuple[float, float, dict[str, Any]]] = []
    for chunk_items in results:
        for start, end, item in chunk_items:
            try:
                confidence = float(item.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < MIN_DEMO_CONFIDENCE or end - start < MIN_DEMO_SECONDS:
                continue
            item = {**item, "confidence": max(0.0, min(1.0, confidence))}
            if spans and start - spans[-1][1] <= DEMO_MERGE_GAP:
                previous_start, previous_end, previous_item = spans[-1]
                spans[-1] = (previous_start, max(previous_end, end), previous_item)
                continue
            spans.append((start, end, item))

    moments = [
        DemoMoment(
            id=f"demo_{index:03d}",
            source_start=round(start, 3),
            source_end=round(end, 3),
            kind=str(item.get("kind", "other")).strip() or "other",
            description=str(item.get("description", "")).strip(),
            confidence=float(item["confidence"]),
        )
        for index, (start, end, item) in enumerate(spans, start=1)
    ]
    for moment in moments:
        logger(
            f"Demo {moment.id}: {format_timestamp(moment.source_start)}-"
            f"{format_timestamp(moment.source_end)} {moment.kind} - {moment.description}"
        )
    return moments


def demo_moments_json(moments: Sequence[DemoMoment]) -> str:
    return json.dumps([moment.to_dict() for moment in moments], ensure_ascii=False, indent=2)
