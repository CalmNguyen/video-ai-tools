from __future__ import annotations

import json
from collections import defaultdict
from typing import Callable, Sequence

from .gemini_client import GeminiJsonClient
from .models import Cue, SemanticAlignment


MAX_TARGET_CHUNK_CUES = 80
MAX_TARGET_CHUNK_CHARS = 24_000
FULL_SOURCE_PROMPT_CHARS = 55_000


def _cue_chunks(cues: Sequence[Cue]) -> list[list[Cue]]:
    chunks: list[list[Cue]] = []
    current: list[Cue] = []
    current_chars = 0
    for cue in cues:
        cue_chars = len(cue.text) + 80
        if current and (
            len(current) >= MAX_TARGET_CHUNK_CUES
            or current_chars + cue_chars > MAX_TARGET_CHUNK_CHARS
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(cue)
        current_chars += cue_chars
    if current:
        chunks.append(current)
    return chunks


def _source_candidates(
    source_cues: Sequence[Cue],
    target_chunk: Sequence[Cue],
    target_duration: float,
) -> list[Cue]:
    total_chars = sum(len(cue.text) + 80 for cue in source_cues)
    if total_chars <= FULL_SOURCE_PROMPT_CHARS or not source_cues:
        return list(source_cues)
    source_duration = source_cues[-1].end
    start_ratio = target_chunk[0].start / max(0.001, target_duration)
    end_ratio = target_chunk[-1].end / max(0.001, target_duration)
    margin = max(180.0, source_duration * 0.12)
    window_start = max(0.0, source_duration * start_ratio - margin)
    window_end = min(source_duration, source_duration * end_ratio + margin)
    candidates = [
        cue for cue in source_cues if cue.end > window_start and cue.start < window_end
    ]
    return candidates or list(source_cues)


def _parse_ids(value: object, valid_ids: set[int]) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    result: list[int] = []
    for raw in value:
        try:
            cue_id = int(raw)
        except (TypeError, ValueError):
            continue
        if cue_id in valid_ids and cue_id not in result:
            result.append(cue_id)
    return tuple(result)


def validate_alignments(
    payloads: Sequence[object],
    source_cues: Sequence[Cue],
    target_cues: Sequence[Cue],
    logger: Callable[[str], None],
) -> tuple[list[SemanticAlignment], dict[str, object]]:
    source_ids = {cue.id for cue in source_cues}
    target_ids = {cue.id for cue in target_cues}
    candidates: list[SemanticAlignment] = []
    invalid_count = 0
    for payload in payloads:
        raw_items = payload.get("alignments", []) if isinstance(payload, dict) else []
        if not isinstance(raw_items, list):
            invalid_count += 1
            continue
        for item in raw_items:
            if not isinstance(item, dict):
                invalid_count += 1
                continue
            target = _parse_ids(item.get("targetCueIds"), target_ids)
            source = _parse_ids(item.get("sourceCueIds"), source_ids)
            if not target or not source:
                invalid_count += 1
                continue
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            candidates.append(
                SemanticAlignment(
                    target_cue_ids=target,
                    source_cue_ids=source,
                    confidence=max(0.0, min(1.0, confidence)),
                    reason=str(item.get("reason", "")).strip(),
                )
            )

    # Target cues should have one authoritative mapping. Higher-confidence groups win.
    claimed: set[int] = set()
    accepted: list[SemanticAlignment] = []
    for alignment in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        remaining = tuple(cue_id for cue_id in alignment.target_cue_ids if cue_id not in claimed)
        if not remaining:
            continue
        accepted.append(
            SemanticAlignment(
                target_cue_ids=remaining,
                source_cue_ids=alignment.source_cue_ids,
                confidence=alignment.confidence,
                reason=alignment.reason,
            )
        )
        claimed.update(remaining)
    accepted.sort(key=lambda item: min(item.target_cue_ids))
    unmatched = sorted(target_ids - claimed)
    low_confidence = [
        cue_id
        for item in accepted
        if item.confidence < 0.65
        for cue_id in item.target_cue_ids
    ]
    if invalid_count:
        logger(f"Discarded {invalid_count} invalid semantic mappings.")
    if low_confidence:
        logger(
            "Low-confidence target cue IDs: " + ", ".join(map(str, low_confidence[:30]))
        )
    if unmatched:
        logger("Unmatched target cue IDs: " + ", ".join(map(str, unmatched[:30])))
    report: dict[str, object] = {
        "targetCueCount": len(target_cues),
        "matchedTargetCueCount": len(claimed),
        "unmatchedTargetCueIds": unmatched,
        "lowConfidenceTargetCueIds": low_confidence,
        "invalidMappingsRemoved": invalid_count,
    }
    return accepted, report


class SemanticAligner:
    def __init__(self, client: GeminiJsonClient, logger: Callable[[str], None]):
        self.client = client
        self.logger = logger

    def align(
        self,
        original_script_context: str,
        source_cues: Sequence[Cue],
        target_cues: Sequence[Cue],
        target_duration: float,
    ) -> tuple[list[SemanticAlignment], dict[str, object]]:
        chunks = _cue_chunks(target_cues)
        payloads: list[object] = []
        for index, target_chunk in enumerate(chunks, start=1):
            candidates = _source_candidates(source_cues, target_chunk, target_duration)
            prompt = f"""
You are a semantic subtitle alignment engine for a professional video editor.
Match meaning between the SOURCE transcript (original footage) and TARGET
transcript (new narration timeline). The languages may differ.

Rules:
- Never align by subtitle index. Use semantic meaning.
- Support 1-to-1, many-to-1, 1-to-many, and many-to-many mappings.
- A target cue may be omitted when no genuine source match exists.
- Use ONLY cue IDs present below. Never invent IDs or timestamps.
- Keep related adjacent cues together when that expresses one idea/action.
- confidence is 0.0 to 1.0.
- Return JSON only: {{"alignments":[{{"targetCueIds":[1],
  "sourceCueIds":[2,3],"confidence":0.95,"reason":"brief semantic reason"}}]}}

ORIGINAL SCRIPT CONTEXT:
{original_script_context}

SOURCE CUES AVAILABLE FOR THIS CHUNK:
{json.dumps([cue.to_dict() for cue in candidates], ensure_ascii=False)}

TARGET CUES TO ALIGN (chunk {index}/{len(chunks)}):
{json.dumps([cue.to_dict() for cue in target_chunk], ensure_ascii=False)}
""".strip()
            payloads.append(
                self.client.generate_json(
                    prompt,
                    f"semantic alignment {index}/{len(chunks)}",
                    max_output_tokens=24576,
                )
            )
        return validate_alignments(payloads, source_cues, target_cues, self.logger)


def source_ids_by_target(
    alignments: Sequence[SemanticAlignment],
) -> dict[int, list[int]]:
    result: dict[int, list[int]] = defaultdict(list)
    for alignment in alignments:
        for target_id in alignment.target_cue_ids:
            for source_id in alignment.source_cue_ids:
                if source_id not in result[target_id]:
                    result[target_id].append(source_id)
    return dict(result)
