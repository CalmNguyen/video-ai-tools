from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Cue:
    id: int
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False

    def to_dict(self, include_path: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "hasAudio": self.has_audio,
        }
        if include_path:
            result["path"] = str(self.path)
        return result


@dataclass(frozen=True)
class Asset:
    id: str
    filename: str
    path: Path
    type: str
    duration: float | None = None
    width: int = 0
    height: int = 0

    def to_dict(self, include_path: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "filename": self.filename,
            "type": self.type,
        }
        if self.duration is not None:
            result["duration"] = round(self.duration, 3)
        if self.width and self.height:
            result["width"] = self.width
            result["height"] = self.height
        if include_path:
            result["path"] = str(self.path)
        return result


@dataclass(frozen=True)
class SemanticAlignment:
    target_cue_ids: tuple[int, ...]
    source_cue_ids: tuple[int, ...]
    confidence: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetCueIds": list(self.target_cue_ids),
            "sourceCueIds": list(self.source_cue_ids),
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


@dataclass
class EditSegment:
    id: str
    target_start: float
    target_end: float
    target_cue_ids: list[int]
    source_cue_ids: list[int]
    visual: dict[str, Any]
    overlay: dict[str, Any] = field(default_factory=lambda: {"type": "none"})
    transition_out: dict[str, Any] = field(
        default_factory=lambda: {"type": "cut", "duration": 0}
    )
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.target_end - self.target_start)

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_id: str) -> "EditSegment":
        def number(name: str, default: float = 0.0) -> float:
            try:
                return float(data.get(name, default))
            except (TypeError, ValueError):
                return default

        def ids(name: str) -> list[int]:
            result: list[int] = []
            raw = data.get(name, [])
            if not isinstance(raw, list):
                return result
            for value in raw:
                try:
                    cue_id = int(value)
                except (TypeError, ValueError):
                    continue
                if cue_id not in result:
                    result.append(cue_id)
            return result

        visual = data.get("visual")
        overlay = data.get("overlay")
        transition = data.get("transitionOut")
        return cls(
            id=str(data.get("id", fallback_id)).strip() or fallback_id,
            target_start=number("targetStart"),
            target_end=number("targetEnd"),
            target_cue_ids=ids("targetCueIds"),
            source_cue_ids=ids("sourceCueIds"),
            visual=dict(visual) if isinstance(visual, dict) else {},
            overlay=dict(overlay) if isinstance(overlay, dict) else {"type": "none"},
            transition_out=(
                dict(transition)
                if isinstance(transition, dict)
                else {"type": "cut", "duration": 0}
            ),
            reason=str(data.get("reason", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "targetStart": round(self.target_start, 3),
            "targetEnd": round(self.target_end, 3),
            "targetCueIds": self.target_cue_ids,
            "sourceCueIds": self.source_cue_ids,
            "visual": self.visual,
            "overlay": self.overlay,
            "transitionOut": self.transition_out,
            "reason": self.reason,
        }


@dataclass
class EditPlan:
    version: int
    target_duration: float
    segments: list[EditSegment]
    project: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditPlan":
        if not isinstance(data, dict):
            raise ValueError("Edit plan must be a JSON object.")
        try:
            duration = float(data.get("targetDuration", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("targetDuration must be a number.") from exc
        raw_segments = data.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("Edit plan is missing a segments array.")
        segments = [
            EditSegment.from_dict(item, f"seg_{index:03d}")
            for index, item in enumerate(raw_segments, start=1)
            if isinstance(item, dict)
        ]
        project = data.get("project")
        return cls(
            version=int(data.get("version", 1)),
            target_duration=duration,
            segments=segments,
            project=dict(project) if isinstance(project, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": self.version,
            "targetDuration": round(self.target_duration, 3),
            "segments": [segment.to_dict() for segment in self.segments],
        }
        if self.project:
            result["project"] = self.project
        return result


@dataclass(frozen=True)
class RunConfig:
    api_key: str
    model: str
    source_video: Path
    source_srt: Path
    original_script: Path
    target_voice: Path
    target_srt: Path
    assets_folder: Path | None
    output_dir: Path
    resolution: str
    fps: int
    burn_subtitles: bool
    export_srt: bool
    original_audio_mode: str
    original_audio_volume: float
    normalize_audio: bool
    max_speed_up: float
    max_slow_down: float
    extra_instructions: str
    subtitle_font_size: int = 14

    @property
    def output_size(self) -> tuple[int, int]:
        try:
            width, height = self.resolution.lower().split("x", 1)
            return int(width), int(height)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid output resolution: {self.resolution}") from exc

    def project_dict(self) -> dict[str, Any]:
        return {
            "sourceVideo": str(self.source_video),
            "sourceSrt": str(self.source_srt),
            "originalScript": str(self.original_script),
            "targetVoice": str(self.target_voice),
            "targetSrt": str(self.target_srt),
            "assetsFolder": str(self.assets_folder) if self.assets_folder else "",
            "outputResolution": self.resolution,
            "fps": self.fps,
            "burnSubtitles": self.burn_subtitles,
            "exportSrt": self.export_srt,
            "subtitleFontSize": self.subtitle_font_size,
            "originalAudioMode": self.original_audio_mode,
            "originalAudioVolume": round(self.original_audio_volume, 3),
            "normalizeAudio": self.normalize_audio,
            "maxSpeedUp": round(self.max_speed_up, 3),
            "maxSlowDown": round(self.max_slow_down, 3),
        }
