from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Sequence

from .models import Asset, Cue, DemoInsert, EditPlan, EditSegment, MediaInfo


ALLOWED_VISUAL_TYPES = {
    "source_video",
    "source_freeze_frame",
    "asset_image",
    "asset_video",
    "solid_background",
}
ALLOWED_ANIMATIONS = {"none", "fade", "pop"}
ALLOWED_POSITIONS = {"top", "center", "bottom"}
EPSILON = 0.02
MIN_DEMO_INSERT_SECONDS = 0.5
MAX_DEMO_INSERT_SECONDS = 120.0


def load_plan(path: Path) -> EditPlan:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Edit plan is not valid JSON: {exc}") from exc
    return EditPlan.from_dict(payload)


def save_plan(plan: EditPlan, path: Path) -> None:
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _number(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_source_time(segment: EditSegment, source_cues: Sequence[Cue]) -> float:
    wanted = set(segment.source_cue_ids)
    matched = [cue for cue in source_cues if cue.id in wanted]
    return matched[0].start if matched else 0.0


def _fallback_segment(start: float, end: float, previous: EditSegment | None) -> EditSegment:
    visual: dict[str, Any] = {"type": "solid_background", "color": "#111111"}
    if previous is not None:
        previous_visual = previous.visual
        previous_type = previous_visual.get("type")
        if previous_type == "source_video":
            visual = {
                "type": "source_freeze_frame",
                "sourceTime": _number(previous_visual.get("sourceEnd"), 0.0),
            }
        elif previous_type == "source_freeze_frame":
            visual = {
                "type": "source_freeze_frame",
                "sourceTime": _number(previous_visual.get("sourceTime"), 0.0),
            }
    return EditSegment(
        id="auto_gap",
        target_start=start,
        target_end=end,
        target_cue_ids=[],
        source_cue_ids=[],
        visual=visual,
        reason="Automatically filled an uncovered target timeline gap.",
    )


def _safe_insert_points(target_cues: Sequence[Cue], target_duration: float) -> list[float]:
    """Target times that do not fall inside any spoken cue."""
    points = {0.0, target_duration}
    for cue in target_cues:
        points.update((cue.start, cue.end))
    return sorted(
        point
        for point in points
        if 0.0 <= point <= target_duration
        and not any(cue.start + EPSILON < point < cue.end - EPSILON for cue in target_cues)
    )


def _cue_ids_between(target_cues: Sequence[Cue], start: float, end: float) -> list[int]:
    return [cue.id for cue in target_cues if cue.end > start + EPSILON and cue.start < end - EPSILON]


class PlanValidator:
    def __init__(self, logger: Callable[[str], None]):
        self.logger = logger
        self.warnings: list[str] = []
        self.repairs = 0
        self.speed_clamps = 0

    def _warn(self, message: str) -> None:
        self.warnings.append(message)
        self.logger(message)

    def validate(
        self,
        plan: EditPlan,
        source_info: MediaInfo,
        target_audio_duration: float,
        source_cues: Sequence[Cue],
        target_cues: Sequence[Cue],
        assets: Sequence[Asset],
        max_speed_up: float,
        max_slow_down: float,
        allow_demo_inserts: bool = False,
    ) -> tuple[EditPlan, dict[str, Any]]:
        if not plan.segments:
            raise ValueError("Edit plan has no segments; refusing to render a blank plan.")
        if target_audio_duration <= 0:
            raise ValueError("Target voice duration must be greater than zero.")
        if not 1.0 <= max_speed_up <= 2.0:
            raise ValueError("Max Speed Up must be between 1.0 and 2.0.")
        if not 0.5 <= max_slow_down <= 1.0:
            raise ValueError("Max Slow Down must be between 0.5 and 1.0.")

        self.warnings = []
        self.repairs = 0
        self.speed_clamps = 0
        source_ids = {cue.id for cue in source_cues}
        target_ids = {cue.id for cue in target_cues}
        asset_map = {asset.id: asset for asset in assets}
        plan.target_duration = round(target_audio_duration, 3)
        ordered = sorted(plan.segments, key=lambda item: (item.target_start, item.target_end))
        accepted: list[EditSegment] = []
        cursor = 0.0

        for original in ordered:
            segment = EditSegment.from_dict(original.to_dict(), original.id)
            if segment.target_end <= segment.target_start + EPSILON:
                self._warn(f"Removed {segment.id}: target interval is empty or reversed.")
                self.repairs += 1
                continue
            if segment.target_end <= 0 or segment.target_start >= target_audio_duration:
                self._warn(f"Removed {segment.id}: target interval is outside target audio.")
                self.repairs += 1
                continue
            segment.target_start = max(0.0, segment.target_start)
            segment.target_end = min(target_audio_duration, segment.target_end)

            if segment.target_start < cursor - EPSILON:
                old_start = segment.target_start
                segment.target_start = cursor
                self._warn(
                    f"Repaired overlap in {segment.id}: targetStart "
                    f"{old_start:.3f} -> {cursor:.3f}."
                )
                self.repairs += 1
                if segment.target_end <= segment.target_start + EPSILON:
                    self._warn(f"Removed {segment.id}: fully covered by an earlier segment.")
                    continue
            if segment.target_start > cursor + EPSILON:
                gap = _fallback_segment(cursor, segment.target_start, accepted[-1] if accepted else None)
                self._validate_visual(
                    gap,
                    source_info,
                    source_cues,
                    asset_map,
                    max_speed_up,
                    max_slow_down,
                )
                accepted.append(gap)
                self._warn(
                    f"Filled target gap {cursor:.3f}-{segment.target_start:.3f} "
                    f"({segment.target_start - cursor:.3f}s)."
                )
                self.repairs += 1

            segment.target_cue_ids = [
                cue_id for cue_id in segment.target_cue_ids if cue_id in target_ids
            ]
            segment.source_cue_ids = [
                cue_id for cue_id in segment.source_cue_ids if cue_id in source_ids
            ]
            if not segment.target_cue_ids:
                segment.target_cue_ids = [
                    cue.id
                    for cue in target_cues
                    if cue.end > segment.target_start and cue.start < segment.target_end
                ]
            self._validate_visual(
                segment,
                source_info,
                source_cues,
                asset_map,
                max_speed_up,
                max_slow_down,
            )
            self._validate_overlay(segment)
            segment.transition_out = {"type": "cut", "duration": 0}
            accepted.append(segment)
            cursor = segment.target_end

        if cursor < target_audio_duration - EPSILON:
            trailing_gap = _fallback_segment(
                cursor, target_audio_duration, accepted[-1] if accepted else None
            )
            self._validate_visual(
                trailing_gap,
                source_info,
                source_cues,
                asset_map,
                max_speed_up,
                max_slow_down,
            )
            accepted.append(trailing_gap)
            self._warn(
                f"Filled trailing target gap {cursor:.3f}-{target_audio_duration:.3f} "
                f"({target_audio_duration - cursor:.3f}s)."
            )
            self.repairs += 1
        if not accepted:
            raise ValueError("No valid edit segments remain after validation.")

        demo_inserts = self._validate_demo_inserts(
            plan.demo_inserts,
            source_info,
            target_audio_duration,
            target_cues,
            allow_demo_inserts,
        )
        accepted = self._split_at_inserts(accepted, demo_inserts, target_cues)

        for index, segment in enumerate(accepted, start=1):
            segment.id = f"seg_{index:03d}"
            segment.target_start = round(segment.target_start, 3)
            segment.target_end = round(segment.target_end, 3)
        validated = EditPlan(
            version=1,
            target_duration=round(target_audio_duration, 3),
            segments=accepted,
            project=plan.project,
            demo_inserts=demo_inserts,
        )
        report = self._report(validated, target_cues)
        return validated, report

    def _validate_demo_inserts(
        self,
        inserts: Sequence[DemoInsert],
        source_info: MediaInfo,
        target_duration: float,
        target_cues: Sequence[Cue],
        allowed: bool,
    ) -> list[DemoInsert]:
        if not inserts:
            return []
        if not allowed:
            self._warn(
                f"Removed {len(inserts)} demo insert(s): keeping original demo audio is disabled."
            )
            self.repairs += 1
            return []
        if not source_info.has_audio:
            self._warn(f"Removed {len(inserts)} demo insert(s): the original video has no audio.")
            self.repairs += 1
            return []
        safe_points = _safe_insert_points(target_cues, target_duration)
        accepted: list[DemoInsert] = []
        used_demo_ids: set[str] = set()
        for raw in sorted(inserts, key=lambda item: item.insert_at):
            item = DemoInsert.from_dict(raw.to_dict(), raw.id)
            start = max(0.0, item.source_start)
            end = min(source_info.duration, item.source_end)
            if end - start < MIN_DEMO_INSERT_SECONDS:
                self._warn(f"Removed {item.id}: source range is empty or too short.")
                self.repairs += 1
                continue
            if end - start > MAX_DEMO_INSERT_SECONDS:
                end = start + MAX_DEMO_INSERT_SECONDS
                self._warn(f"{item.id}: demo trimmed to {MAX_DEMO_INSERT_SECONDS:.0f}s.")
                self.repairs += 1
            if item.demo_id and item.demo_id in used_demo_ids:
                self._warn(f"Removed {item.id}: {item.demo_id} is already inserted.")
                self.repairs += 1
                continue
            if any(start < other.source_end and end > other.source_start for other in accepted):
                self._warn(f"Removed {item.id}: its source range overlaps another demo insert.")
                self.repairs += 1
                continue
            wanted = max(0.0, min(target_duration, item.insert_at))
            insert_at = (
                min(safe_points, key=lambda point: abs(point - wanted)) if safe_points else wanted
            )
            if abs(insert_at - item.insert_at) > 0.05:
                self._warn(
                    f"{item.id}: insertAt {item.insert_at:.3f} snapped to the sentence "
                    f"boundary {insert_at:.3f}."
                )
            item.insert_at = round(insert_at, 3)
            item.source_start = round(start, 3)
            item.source_end = round(end, 3)
            item.volume = round(max(0.0, min(2.0, item.volume)), 3)
            if item.demo_id:
                used_demo_ids.add(item.demo_id)
            accepted.append(item)
        accepted.sort(key=lambda item: (item.insert_at, item.source_start))
        for index, item in enumerate(accepted, start=1):
            item.id = f"demo_insert_{index:03d}"
        return accepted

    def _split_at_inserts(
        self,
        segments: list[EditSegment],
        inserts: Sequence[DemoInsert],
        target_cues: Sequence[Cue],
    ) -> list[EditSegment]:
        """Make every insert point a segment boundary so the insert is a clean cut."""
        result = list(segments)
        for item in inserts:
            for index, segment in enumerate(result):
                if segment.target_start + EPSILON < item.insert_at < segment.target_end - EPSILON:
                    first, second = self._split_segment(
                        segment, item.insert_at - segment.target_start, target_cues
                    )
                    result[index : index + 1] = [first, second]
                    break
        return result

    def _split_segment(
        self, segment: EditSegment, offset: float, target_cues: Sequence[Cue]
    ) -> tuple[EditSegment, EditSegment]:
        first = EditSegment.from_dict(segment.to_dict(), segment.id)
        second = EditSegment.from_dict(segment.to_dict(), segment.id + "b")
        first.target_end = segment.target_start + offset
        second.target_start = first.target_end
        first.target_cue_ids = _cue_ids_between(target_cues, first.target_start, first.target_end)
        second.target_cue_ids = _cue_ids_between(
            target_cues, second.target_start, second.target_end
        )

        visual_type = segment.visual.get("type")
        if visual_type in {"source_video", "asset_video"}:
            start_key, end_key = (
                ("sourceStart", "sourceEnd")
                if visual_type == "source_video"
                else ("assetStart", "assetEnd")
            )
            range_start = _number(segment.visual.get(start_key), 0.0)
            range_end = _number(segment.visual.get(end_key), range_start)
            speed = max(EPSILON, _number(segment.visual.get("playbackSpeed"), 1.0))
            played = (range_end - range_start) / speed
            if offset < played - EPSILON:
                middle = round(range_start + offset * speed, 3)
                first.visual[end_key] = middle
                first.visual["holdLastFrameSeconds"] = 0.0
                second.visual[start_key] = middle
            else:
                first.visual["holdLastFrameSeconds"] = round(offset - played, 3)
                if visual_type == "source_video":
                    second.visual = {
                        "type": "source_freeze_frame",
                        "sourceTime": round(max(0.0, range_end - 0.05), 3),
                        "fitMode": self._fit_mode(segment.visual),
                    }
                else:
                    tail_start = max(range_start, range_end - 0.1)
                    second.visual[start_key] = round(tail_start, 3)
                    second.visual["playbackSpeed"] = 1.0
                    second.visual["holdLastFrameSeconds"] = round(
                        max(0.0, second.duration - (range_end - tail_start)), 3
                    )
        for part in (first, second):
            if isinstance(part.visual.get("zoom"), dict):
                part.visual["zoom"] = {**part.visual["zoom"], "enabled": False}
            if isinstance(part.visual.get("motion"), dict):
                part.visual["motion"] = {**part.visual["motion"], "type": "none"}

        overlay = segment.overlay
        if overlay.get("type") == "text":
            overlay_start = _number(overlay.get("startOffset"), 0.0)
            overlay_end = overlay_start + _number(overlay.get("duration"), segment.duration)
            first.overlay = {"type": "none"}
            second.overlay = {"type": "none"}
            if overlay_start < offset - EPSILON:
                first.overlay = {
                    **overlay,
                    "duration": round(min(overlay_end, offset) - overlay_start, 3),
                }
            if overlay_end > offset + EPSILON:
                new_start = max(overlay_start, offset) - offset
                second.overlay = {
                    **overlay,
                    "startOffset": round(new_start, 3),
                    "duration": round(overlay_end - offset - new_start, 3),
                }
        self.logger(f"Split {segment.id} at {first.target_end:.3f}s for a demo insert.")
        return first, second

    def _validate_visual(
        self,
        segment: EditSegment,
        source_info: MediaInfo,
        source_cues: Sequence[Cue],
        assets: dict[str, Asset],
        max_speed_up: float,
        max_slow_down: float,
    ) -> None:
        visual_type = str(segment.visual.get("type", "")).strip()
        if visual_type not in ALLOWED_VISUAL_TYPES:
            self._warn(
                f"{segment.id}: unsupported visual type '{visual_type}' changed to solid_background."
            )
            segment.visual = {"type": "solid_background", "color": "#111111"}
            self.repairs += 1
            return
        if visual_type == "source_video":
            self._validate_source_video(
                segment, source_info, source_cues, max_speed_up, max_slow_down
            )
        elif visual_type == "source_freeze_frame":
            source_time = _number(
                segment.visual.get("sourceTime"), _default_source_time(segment, source_cues)
            )
            frame_margin = max(0.04, 1.0 / max(1.0, source_info.fps))
            latest_frame = max(0.0, source_info.duration - frame_margin)
            segment.visual = {
                "type": "source_freeze_frame",
                "sourceTime": round(max(0.0, min(latest_frame, source_time)), 3),
                "fitMode": self._fit_mode(segment.visual),
            }
        elif visual_type in {"asset_image", "asset_video"}:
            self._validate_asset_visual(
                segment, assets, max_speed_up, max_slow_down
            )
        else:
            raw_color = str(segment.visual.get("color", "#111111"))
            color = raw_color if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw_color) else "#111111"
            segment.visual = {"type": "solid_background", "color": color}

    @staticmethod
    def _fit_mode(visual: dict[str, Any]) -> str:
        return "cover" if str(visual.get("fitMode", "contain")) == "cover" else "contain"

    def _validate_source_video(
        self,
        segment: EditSegment,
        source_info: MediaInfo,
        source_cues: Sequence[Cue],
        max_speed_up: float,
        max_slow_down: float,
    ) -> None:
        matched = [cue for cue in source_cues if cue.id in set(segment.source_cue_ids)]
        default_start = min((cue.start for cue in matched), default=0.0)
        default_end = max((cue.end for cue in matched), default=0.0)
        source_start = max(0.0, _number(segment.visual.get("sourceStart"), default_start))
        source_end = min(
            source_info.duration,
            _number(segment.visual.get("sourceEnd"), default_end),
        )
        if source_end <= source_start + EPSILON:
            fallback_time = max(0.0, min(source_info.duration, default_start))
            self._warn(
                f"{segment.id}: invalid source range changed to a freeze frame at "
                f"{fallback_time:.3f}."
            )
            segment.visual = {
                "type": "source_freeze_frame",
                "sourceTime": round(fallback_time, 3),
                "fitMode": self._fit_mode(segment.visual),
            }
            self.repairs += 1
            return
        target_duration = segment.duration
        source_duration = source_end - source_start
        natural_speed = source_duration / max(EPSILON, target_duration)
        hold = 0.0
        if natural_speed > max_speed_up:
            old_speed = natural_speed
            source_end = min(source_info.duration, source_start + target_duration * max_speed_up)
            natural_speed = (source_end - source_start) / target_duration
            self._warn(
                f"{segment.id}: speed clamped {old_speed:.3f}x -> {natural_speed:.3f}x "
                "by trimming the source range."
            )
            self.speed_clamps += 1
        elif natural_speed < max_slow_down:
            old_speed = natural_speed
            natural_speed = max_slow_down
            played_duration = source_duration / natural_speed
            hold = max(0.0, target_duration - played_duration)
            self._warn(
                f"{segment.id}: speed clamped {old_speed:.3f}x -> {natural_speed:.3f}x; "
                f"the last frame will hold for {hold:.3f}s."
            )
            self.speed_clamps += 1
        raw_zoom = segment.visual.get("zoom")
        zoom = self._validate_zoom(raw_zoom if isinstance(raw_zoom, dict) else {})
        segment.visual = {
            "type": "source_video",
            "sourceStart": round(source_start, 3),
            "sourceEnd": round(source_end, 3),
            "fitMode": self._fit_mode(segment.visual),
            "speedStrategy": "fit",
            "playbackSpeed": round(natural_speed, 4),
            "holdLastFrameSeconds": round(hold, 3),
            "crop": {"mode": "center"},
            "zoom": zoom,
        }

    @staticmethod
    def _validate_zoom(raw: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(raw.get("enabled", False))
        start = max(1.0, min(1.2, _number(raw.get("startScale"), 1.0)))
        end = max(1.0, min(1.2, _number(raw.get("endScale"), 1.05)))
        return {
            "enabled": enabled,
            "startScale": round(start, 3),
            "endScale": round(end, 3),
        }

    def _validate_asset_visual(
        self,
        segment: EditSegment,
        assets: dict[str, Asset],
        max_speed_up: float,
        max_slow_down: float,
    ) -> None:
        asset_id = str(segment.visual.get("assetId", ""))
        asset = assets.get(asset_id)
        expected_type = "image" if segment.visual.get("type") == "asset_image" else "video"
        if asset is None or asset.type != expected_type:
            self._warn(
                f"{segment.id}: unknown/incompatible asset '{asset_id}' changed to solid background."
            )
            segment.visual = {"type": "solid_background", "color": "#111111"}
            self.repairs += 1
            return
        if expected_type == "image":
            raw_motion = segment.visual.get("motion")
            motion = raw_motion if isinstance(raw_motion, dict) else {}
            motion_type = "slow_zoom" if motion.get("type") == "slow_zoom" else "none"
            segment.visual = {
                "type": "asset_image",
                "assetId": asset.id,
                "fitMode": self._fit_mode(segment.visual),
                "motion": {
                    "type": motion_type,
                    "startScale": round(max(1.0, min(1.2, _number(motion.get("startScale"), 1.0))), 3),
                    "endScale": round(max(1.0, min(1.2, _number(motion.get("endScale"), 1.05))), 3),
                },
            }
            return
        duration = asset.duration or 0.0
        asset_start = max(0.0, _number(segment.visual.get("assetStart"), 0.0))
        asset_end = min(duration, _number(segment.visual.get("assetEnd"), duration))
        if asset_end <= asset_start + EPSILON:
            self._warn(f"{segment.id}: invalid asset video range changed to solid background.")
            segment.visual = {"type": "solid_background", "color": "#111111"}
            self.repairs += 1
            return
        source_duration = asset_end - asset_start
        natural_speed = source_duration / max(EPSILON, segment.duration)
        hold = 0.0
        if natural_speed > max_speed_up:
            old_speed = natural_speed
            asset_end = min(duration, asset_start + segment.duration * max_speed_up)
            natural_speed = (asset_end - asset_start) / segment.duration
            self._warn(
                f"{segment.id}: asset speed clamped {old_speed:.3f}x -> {natural_speed:.3f}x."
            )
            self.speed_clamps += 1
        elif natural_speed < max_slow_down:
            old_speed = natural_speed
            natural_speed = max_slow_down
            hold = max(0.0, segment.duration - source_duration / natural_speed)
            self._warn(
                f"{segment.id}: asset speed clamped {old_speed:.3f}x -> "
                f"{natural_speed:.3f}x with {hold:.3f}s frame hold."
            )
            self.speed_clamps += 1
        segment.visual = {
            "type": "asset_video",
            "assetId": asset.id,
            "assetStart": round(asset_start, 3),
            "assetEnd": round(asset_end, 3),
            "fitMode": self._fit_mode(segment.visual),
            "speedStrategy": "fit",
            "playbackSpeed": round(natural_speed, 4),
            "holdLastFrameSeconds": round(hold, 3),
        }

    def _validate_overlay(self, segment: EditSegment) -> None:
        if str(segment.overlay.get("type", "none")) != "text":
            segment.overlay = {"type": "none"}
            return
        text = re.sub(r"\s+", " ", str(segment.overlay.get("text", ""))).strip()[:120]
        if not text:
            segment.overlay = {"type": "none"}
            return
        start_offset = max(0.0, min(segment.duration, _number(segment.overlay.get("startOffset"), 0.0)))
        duration = max(
            0.0,
            min(
                segment.duration - start_offset,
                _number(segment.overlay.get("duration"), segment.duration - start_offset),
            ),
        )
        if duration <= EPSILON:
            segment.overlay = {"type": "none"}
            return
        position = str(segment.overlay.get("position", "top"))
        animation = str(segment.overlay.get("animation", "fade"))
        segment.overlay = {
            "type": "text",
            "text": text,
            "startOffset": round(start_offset, 3),
            "duration": round(duration, 3),
            "position": position if position in ALLOWED_POSITIONS else "top",
            "animation": animation if animation in ALLOWED_ANIMATIONS else "fade",
        }

    def _report(self, plan: EditPlan, target_cues: Sequence[Cue]) -> dict[str, Any]:
        duration = max(EPSILON, plan.target_duration)
        totals = {
            "source_video": 0.0,
            "source_freeze_frame": 0.0,
            "assets": 0.0,
            "fallback": 0.0,
        }
        speeds: list[tuple[float, float]] = []
        covered_target_ids: set[int] = set()
        for segment in plan.segments:
            visual_type = segment.visual.get("type")
            if visual_type == "source_video":
                totals["source_video"] += segment.duration
                speeds.append(
                    (_number(segment.visual.get("playbackSpeed"), 1.0), segment.duration)
                )
            elif visual_type == "source_freeze_frame":
                totals["source_freeze_frame"] += segment.duration
            elif visual_type in {"asset_image", "asset_video"}:
                totals["assets"] += segment.duration
            else:
                totals["fallback"] += segment.duration
            covered_target_ids.update(segment.target_cue_ids)
        target_ids = {cue.id for cue in target_cues}
        speed_weight = sum(weight for _, weight in speeds)
        average_speed = (
            sum(speed * weight for speed, weight in speeds) / speed_weight
            if speed_weight
            else 0.0
        )
        return {
            "targetCueCount": len(target_ids),
            "coveredTargetCueCount": len(target_ids & covered_target_ids),
            "uncoveredTargetCueIds": sorted(target_ids - covered_target_ids),
            "editSegmentCount": len(plan.segments),
            "sourceVideoPercent": round(100 * totals["source_video"] / duration, 1),
            "freezeFramePercent": round(100 * totals["source_freeze_frame"] / duration, 1),
            "assetPercent": round(100 * totals["assets"] / duration, 1),
            "fallbackPercent": round(100 * totals["fallback"] / duration, 1),
            "averageSourceSpeed": round(average_speed, 3),
            "maximumSourceSpeed": round(max((speed for speed, _ in speeds), default=0.0), 3),
            "demoInsertCount": len(plan.demo_inserts),
            "demoAudioSeconds": round(plan.demo_duration, 3),
            "finalDuration": round(plan.final_duration, 3),
            "repairs": self.repairs,
            "speedClamps": self.speed_clamps,
            "warnings": self.warnings,
        }


def format_validation_report(report: dict[str, Any]) -> str:
    return (
        f"Target cues: {report.get('targetCueCount', 0)}\n"
        f"Covered cues: {report.get('coveredTargetCueCount', 0)}\n"
        f"Uncovered: {len(report.get('uncoveredTargetCueIds', []))}\n\n"
        f"Edit segments: {report.get('editSegmentCount', 0)}\n"
        f"Source video reused: {report.get('sourceVideoPercent', 0)}%\n"
        f"Freeze frames: {report.get('freezeFramePercent', 0)}%\n"
        f"Asset footage: {report.get('assetPercent', 0)}%\n"
        f"Fallback: {report.get('fallbackPercent', 0)}%\n"
        f"Average source speed: {report.get('averageSourceSpeed', 0)}x\n"
        f"Maximum source speed: {report.get('maximumSourceSpeed', 0)}x\n"
        f"Demo inserts (original audio): {report.get('demoInsertCount', 0)} "
        f"({report.get('demoAudioSeconds', 0)}s)\n"
        f"Final duration: {report.get('finalDuration', 0)}s\n"
        f"Repairs: {report.get('repairs', 0)}\n"
        f"Speed clamps: {report.get('speedClamps', 0)}"
    )
