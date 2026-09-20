from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import MediaInfo


def ensure_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError(
            "Could not find " + ", ".join(missing) + ". Install FFmpeg and add it to PATH."
        )


def _ratio(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        try:
            return float(value)
        except ValueError:
            return 0.0


def probe_media(path: str | Path) -> MediaInfo:
    media_path = Path(path)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe could not read {media_path.name}.")
    try:
        payload: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid metadata for {media_path.name}.") from exc
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    raw_duration = payload.get("format", {}).get("duration") or video.get("duration") or 0
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    return MediaInfo(
        path=media_path.resolve(),
        duration=max(0.0, duration),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_ratio(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        has_audio=has_audio,
    )


def escape_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return (
        value.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("[", r"\[")
        .replace("]", r"\]")
        .replace(",", r"\,")
        .replace(";", r"\;")
    )


def escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", r"'\''")
