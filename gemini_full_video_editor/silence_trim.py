"""Shorten long pauses in a voice MP3 and cut the MP4 at exactly the same places.

The MP3 is the master timeline: silences are found in it with ffmpeg `silencedetect`,
every pause longer than `min_silence` is shortened to `keep_gap`, and the very same
time ranges are removed from the video so picture and voice stay in sync.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_NOISE_DB = -40.0
DEFAULT_MIN_SILENCE_MS = 400
DEFAULT_KEEP_GAP_MS = 200
DEFAULT_FPS = 30.0
# MP3 encoder padding makes the file a few frames longer than the last silence_end.
EDGE_TOLERANCE = 0.25

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_FFMPEG_VERSION = re.compile(r"ffmpeg version n?(\d+)\.")


@dataclass(frozen=True)
class TrimOptions:
    noise_db: float = DEFAULT_NOISE_DB
    # Only pauses at least this long are shortened.
    min_silence: float = DEFAULT_MIN_SILENCE_MS / 1000
    # Pause length left between two sentences after cutting.
    keep_gap: float = DEFAULT_KEEP_GAP_MS / 1000
    # Also shorten silence before the first and after the last word.
    trim_edges: bool = False


@dataclass(frozen=True)
class TrimPlan:
    duration: float
    silences: list[tuple[float, float]]
    cuts: list[tuple[float, float]]
    keeps: list[tuple[float, float]]
    # Original silence each cut was taken from (same order as `cuts`).
    cut_sources: list[tuple[float, float]]

    @property
    def removed(self) -> float:
        return sum(end - start for start, end in self.cuts)

    @property
    def new_duration(self) -> float:
        return sum(end - start for start, end in self.keeps)


def detect_silences(
    audio: Path, noise_db: float, min_silence: float, duration: float
) -> list[tuple[float, float]]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio),
            "-vn",
            "-af",
            f"silencedetect=noise={noise_db:.1f}dB:d={max(0.01, min_silence):.3f}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise RuntimeError(f"ffmpeg không đọc được audio: {tail[0]}")
    return parse_silences(result.stderr, duration)


def parse_silences(log: str, duration: float) -> list[tuple[float, float]]:
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in log.splitlines():
        if match := _SILENCE_START.search(line):
            start = max(0.0, float(match.group(1)))
        elif (match := _SILENCE_END.search(line)) and start is not None:
            silences.append((start, min(float(match.group(1)), duration)))
            start = None
    if start is not None and start < duration:
        # Silence that runs to the end of the file has no silence_end line.
        silences.append((start, duration))
    return [(s, e) for s, e in silences if e > s]


def _snap(value: float, fps: float) -> float:
    return round(value * fps) / fps


def plan_cuts(
    silences: Sequence[tuple[float, float]],
    duration: float,
    options: TrimOptions,
    fps: float = DEFAULT_FPS,
) -> TrimPlan:
    """Turn silences into cut ranges; every boundary sits on a video frame so
    video and audio pieces have identical lengths and never drift apart."""
    fps = fps if fps > 0 else DEFAULT_FPS
    half = max(0.0, options.keep_gap) / 2
    edge = EDGE_TOLERANCE
    cuts: list[tuple[float, float]] = []
    sources: list[tuple[float, float]] = []
    for start, end in silences:
        if end - start < options.min_silence:
            continue
        at_start = start <= edge
        at_end = end >= duration - edge
        if (at_start or at_end) and not options.trim_edges:
            continue
        # Keep half of the gap after the previous word and half before the next one.
        cut_start = start if at_start else start + half
        cut_end = end if at_end else end - half
        cut_start, cut_end = _snap(cut_start, fps), _snap(cut_end, fps)
        if at_start:
            cut_start = 0.0
        if at_end:
            cut_end = duration
        if cut_end - cut_start >= 1 / fps:
            cuts.append((cut_start, cut_end))
            sources.append((start, end))

    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor:
            keeps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keeps.append((cursor, duration))
    return TrimPlan(
        duration=duration, silences=list(silences), cuts=cuts, keeps=keeps, cut_sources=sources
    )


def analyze(
    audio: Path,
    options: TrimOptions,
    audio_duration: float,
    video_duration: float = 0.0,
    fps: float = DEFAULT_FPS,
) -> TrimPlan:
    """Plan the cuts over max(audio, video) length; if the video runs longer than the
    voice, that tail counts as trailing silence (kept unless trim_edges)."""
    duration = max(audio_duration, video_duration)
    silences = detect_silences(audio, options.noise_db, options.min_silence, audio_duration)
    if duration > audio_duration + 1e-3:
        if silences and silences[-1][1] >= audio_duration - EDGE_TOLERANCE:
            silences[-1] = (silences[-1][0], duration)
        else:
            silences.append((audio_duration, duration))
    return plan_cuts(silences, duration, options, fps)


def build_filter_graph(
    keeps: Sequence[tuple[float, float]],
    *,
    video: bool,
    fps: float,
    audio_input: int,
    duration: float,
    video_pad: float = 0.0,
) -> str:
    """trim/atrim every kept piece and concat them back together. Both streams are
    first padded to `duration` (silence / frozen last frame) so they line up."""
    count = len(keeps)
    lines: list[str] = []
    if video:
        pad = f"tpad=stop_mode=clone:stop_duration={video_pad:.6f}," if video_pad > 0 else ""
        lines.append(
            f"[0:v]fps={fps:.6f},{pad}scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,split={count}"
            + "".join(f"[vs{i}]" for i in range(count))
        )
    lines.append(
        f"[{audio_input}:a]aresample=48000,apad=whole_dur={duration:.6f},asplit={count}"
        + "".join(f"[as{i}]" for i in range(count))
    )
    pieces: list[str] = []
    for i, (start, end) in enumerate(keeps):
        if video:
            lines.append(f"[vs{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]")
            pieces.append(f"[v{i}]")
        lines.append(f"[as{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]")
        pieces.append(f"[a{i}]")
    out = "[vout][aout]" if video else "[aout]"
    lines.append("".join(pieces) + f"concat=n={count}:v={1 if video else 0}:a=1{out}")
    return ";\n".join(lines)


@lru_cache(maxsize=1)
def _ffmpeg_major() -> int:
    try:
        output = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, errors="replace", check=False
        ).stdout
    except OSError:
        return 0
    match = _FFMPEG_VERSION.search(output)
    # Git/nightly builds ("ffmpeg version 2025-01-...") are always recent.
    return int(match.group(1)) if match else 99


def _filter_script_args(script: Path) -> list[str]:
    # A file avoids the Windows command-line limit when there are hundreds of cuts.
    if _ffmpeg_major() >= 7:
        return ["-/filter_complex", str(script)]
    return ["-filter_complex_script", str(script)]


def _run_with_progress(
    command: list[str],
    total: float,
    on_progress: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
) -> tuple[int, str]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        killed = False
        for line in process.stdout:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                killed = True
                break
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and on_progress and total > 0:
                try:
                    on_progress(min(1.0, int(value) / 1_000_000 / total))
                except ValueError:
                    pass
        process.wait()
        if killed:
            raise InterruptedError("Stopped by user.")
        stderr.seek(0)
        return process.returncode, stderr.read()


def render_trimmed(
    plan: TrimPlan,
    audio: Path,
    output: Path,
    *,
    video: Path | None = None,
    video_duration: float = 0.0,
    fps: float = DEFAULT_FPS,
    crf: int = 18,
    audio_bitrate: str = "192k",
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Write the cut MP4 (video + MP3 voice), or a cut MP3 when `video` is None."""
    if not plan.keeps:
        raise RuntimeError("Không còn đoạn nào để giữ lại.")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".part" + output.suffix)
    with tempfile.TemporaryDirectory(prefix="silence_trim_") as tmp:
        script = Path(tmp) / "filter.txt"
        script.write_text(
            build_filter_graph(
                plan.keeps,
                video=video is not None,
                fps=fps,
                audio_input=1 if video else 0,
                duration=plan.duration,
                video_pad=max(0.0, plan.duration - video_duration) if video_duration > 0 else 0.0,
            ),
            encoding="utf-8",
        )
        command = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]
        if video is not None:
            command += ["-i", str(video)]
        command += ["-i", str(audio), *_filter_script_args(script)]
        if video is not None:
            command += [
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", audio_bitrate,
                "-movflags", "+faststart",
            ]
        else:
            command += ["-map", "[aout]", "-c:a", "libmp3lame", "-b:a", audio_bitrate]
        command.append(str(partial))
        try:
            code, log = _run_with_progress(command, plan.new_duration, on_progress, cancel_event)
            if code != 0 or not partial.is_file() or partial.stat().st_size == 0:
                tail = log.strip().splitlines()[-1:] or ["unknown error"]
                raise RuntimeError(f"ffmpeg không cắt được: {tail[0]}")
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
    return output
