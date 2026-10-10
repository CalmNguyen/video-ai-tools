"""Mix a generated voice-over track into a video that already has audio."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .models import Cue, MediaInfo


@dataclass(frozen=True)
class MixOptions:
    original_volume: float = 1.0
    voice_volume: float = 1.0
    # Lower the original audio while the new voice is speaking.
    duck_original: bool = True
    audio_bitrate: str = "192k"


def srt_fingerprint(
    cues: Sequence[Cue], voice: str, params: dict[str, object]
) -> str:
    """Stable hash of everything that changes the generated audio (not the API key)."""
    payload = {
        "cues": [(round(cue.start, 3), round(cue.end, 3), cue.text) for cue in cues],
        "voice": voice.strip(),
        "params": params,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_filter_graph(has_original_audio: bool, options: MixOptions, duration: float) -> str:
    voice = f"[1:a]aresample=48000,volume={options.voice_volume:.3f}"
    if not has_original_audio:
        # Pad with silence so the audio track is as long as the video.
        return f"{voice},apad=whole_dur={duration:.3f}[aout]"
    original = f"[0:a]aresample=48000,volume={options.original_volume:.3f}[orig]"
    if options.duck_original:
        return (
            f"{original};{voice},asplit=2[voice_mix][voice_sc];"
            "[orig][voice_sc]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=400[ducked];"
            "[ducked][voice_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    return (
        f"{original};{voice}[voice_mix];"
        "[orig][voice_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )


def build_ffmpeg_command(
    video: Path,
    voice_audio: Path,
    output: Path,
    info: MediaInfo,
    options: MixOptions,
    *,
    copy_video: bool = True,
) -> list[str]:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(video),
        "-i",
        str(voice_audio),
        "-filter_complex",
        build_filter_graph(info.has_audio, options, info.duration),
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
    ]
    if copy_video:
        command += ["-c:v", "copy"]
    else:
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    command += ["-c:a", "aac", "-b:a", options.audio_bitrate]
    if info.duration > 0:
        command += ["-t", f"{info.duration:.3f}"]
    command += ["-movflags", "+faststart", str(output)]
    return command


def _run(
    command: list[str], cancel_event: threading.Event | None
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if cancel_event is None:
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    # Poll so Stop can kill a long encode.
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.5)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                process.kill()
                process.communicate()
                raise InterruptedError("Stopped by user.") from None


def mix_voiceover(
    video: Path,
    voice_audio: Path,
    output: Path,
    info: MediaInfo,
    options: MixOptions,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Write `output` = video stream copy + (original audio mixed with voice_audio)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".part" + output.suffix)
    last_error = ""
    try:
        # Stream copy is instant; fall back to re-encoding if the codec can't go into MP4.
        for copy_video in (True, False):
            command = build_ffmpeg_command(
                video, voice_audio, partial, info, options, copy_video=copy_video
            )
            result = _run(command, cancel_event)
            if result.returncode == 0 and partial.is_file() and partial.stat().st_size > 0:
                partial.replace(output)
                return output
            last_error = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            last_error = last_error[0]
        raise RuntimeError(f"ffmpeg không ghép được âm thanh: {last_error}")
    finally:
        partial.unlink(missing_ok=True)
