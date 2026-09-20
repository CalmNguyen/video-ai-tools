from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from .gemini_client import GeminiJsonClient
from .models import Asset, Cue, EditPlan, EditSegment, MediaInfo, SemanticAlignment
from .semantic_aligner import _cue_chunks, source_ids_by_target


PLANNER_SYSTEM_PROMPT = """
You are a professional video editor and deterministic timeline planner. You are
NOT rendering video and MUST NOT output FFmpeg commands. Produce an exact,
machine-readable JSON editing plan. The new narration is the MASTER timeline.

Reuse the most contextually relevant original footage by semantic meaning. Do not
stretch the whole original video. Prefer restrained, professional cuts at sentence,
concept, tutorial-action, and visual-state boundaries. For UI actions, prefer the
aligned footage that demonstrates that action. Explanations may use a stable
relevant shot. Avoid excessive zooms, text, effects, and transitions.

Supported visual types only: source_video, source_freeze_frame, asset_image,
asset_video, solid_background. Supported overlay types: none, text. Supported text
animations: none, fade, pop. transitionOut must be a cut with duration 0.

Never invent cue IDs, source timestamps, asset IDs, or assets. Source timestamps
must stay within source duration. Asset ranges must stay within asset duration.
Use moderate speed fitting only; prefer a freeze frame or another cut when source
footage is too short. Text overlays are short keyword/menu/number emphasis, not
complete subtitles. Do not output arbitrary x/y coordinates.
""".strip()


def _relevant_source_cues(
    target_chunk: Sequence[Cue],
    source_cues: Sequence[Cue],
    mapping: dict[int, list[int]],
) -> list[Cue]:
    source_index = {cue.id: index for index, cue in enumerate(source_cues)}
    indexes: set[int] = set()
    for target in target_chunk:
        for source_id in mapping.get(target.id, []):
            if source_id not in source_index:
                continue
            center = source_index[source_id]
            indexes.update(range(max(0, center - 2), min(len(source_cues), center + 3)))
    if not indexes:
        return list(source_cues[: min(len(source_cues), 120)])
    return [source_cues[index] for index in sorted(indexes)]


def _alignment_subset(
    target_chunk: Sequence[Cue], alignments: Sequence[SemanticAlignment]
) -> list[dict[str, Any]]:
    target_ids = {cue.id for cue in target_chunk}
    return [
        item.to_dict()
        for item in alignments
        if target_ids.intersection(item.target_cue_ids)
    ]


class EditPlanner:
    def __init__(self, client: GeminiJsonClient, logger: Callable[[str], None]):
        self.client = client
        self.logger = logger

    def create_plan(
        self,
        original_script_context: str,
        source_cues: Sequence[Cue],
        target_cues: Sequence[Cue],
        alignments: Sequence[SemanticAlignment],
        source_info: MediaInfo,
        target_duration: float,
        assets: Sequence[Asset],
        extra_instructions: str,
        project: dict[str, Any],
    ) -> EditPlan:
        chunks = _cue_chunks(target_cues)
        mapping = source_ids_by_target(alignments)
        all_segments: list[EditSegment] = []
        for index, target_chunk in enumerate(chunks, start=1):
            chunk_start = 0.0 if index == 1 else target_chunk[0].start
            chunk_end = (
                target_duration if index == len(chunks) else chunks[index][0].start
            )
            relevant_source = _relevant_source_cues(target_chunk, source_cues, mapping)
            prompt = f"""
{PLANNER_SYSTEM_PROMPT}

Create segments covering the entire interval {chunk_start:.3f} to {chunk_end:.3f}
without overlaps. Usually use 2-8 seconds per visual decision, but meaning matters
more. All targetStart/targetEnd values are absolute target narration times.

Return JSON only in this exact envelope:
{{"segments":[{{
  "id":"seg_001","targetStart":0.0,"targetEnd":4.8,
  "targetCueIds":[1],"sourceCueIds":[1],
  "visual":{{"type":"source_video","sourceStart":1.2,"sourceEnd":6.0,
    "fitMode":"contain","speedStrategy":"fit",
    "crop":{{"mode":"center"}},
    "zoom":{{"enabled":false,"startScale":1.0,"endScale":1.08}}}},
  "overlay":{{"type":"none"}},
  "transitionOut":{{"type":"cut","duration":0}},
  "reason":"..."
}}]}}

For source_freeze_frame use sourceTime. For asset_image use assetId and optional
motion {{"type":"slow_zoom","startScale":1.0,"endScale":1.05}}. For asset_video
use assetId, assetStart, assetEnd, speedStrategy. For solid_background use color.
Text overlay fields: type, text, startOffset, duration, position (top/center/bottom),
animation (none/fade/pop). fitMode is contain or cover; default contain preserves UI.

SOURCE VIDEO METADATA:
{json.dumps(source_info.to_dict(), ensure_ascii=False)}

TARGET AUDIO DURATION: {target_duration:.3f}

ORIGINAL SCRIPT CONTEXT:
{original_script_context}

SEMANTIC ALIGNMENTS FOR THIS CHUNK:
{json.dumps(_alignment_subset(target_chunk, alignments), ensure_ascii=False)}

RELEVANT SOURCE CUES:
{json.dumps([cue.to_dict() for cue in relevant_source], ensure_ascii=False)}

TARGET CUES IN THIS CHUNK:
{json.dumps([cue.to_dict() for cue in target_chunk], ensure_ascii=False)}

AVAILABLE ASSETS (empty means use none):
{json.dumps([asset.to_dict() for asset in assets], ensure_ascii=False)}

USER INSTRUCTIONS:
{extra_instructions.strip() or '(none)'}
""".strip()
            payload = self.client.generate_json(
                prompt,
                f"edit plan {index}/{len(chunks)}",
                max_output_tokens=32768,
            )
            raw_segments = payload.get("segments") if isinstance(payload, dict) else None
            if not isinstance(raw_segments, list) or not raw_segments:
                raise RuntimeError(f"Gemini edit-plan chunk {index} has no segments.")
            for raw in raw_segments:
                if isinstance(raw, dict):
                    all_segments.append(
                        EditSegment.from_dict(raw, f"seg_{len(all_segments) + 1:03d}")
                    )

        if not all_segments:
            raise RuntimeError("Gemini returned an empty edit plan.")
        all_segments.sort(key=lambda segment: (segment.target_start, segment.target_end))
        for index, segment in enumerate(all_segments, start=1):
            segment.id = f"seg_{index:03d}"
        return EditPlan(
            version=1,
            target_duration=target_duration,
            segments=all_segments,
            project=project,
        )
