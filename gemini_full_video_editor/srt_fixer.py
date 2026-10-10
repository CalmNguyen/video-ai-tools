from __future__ import annotations

import difflib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from .gemini_client import GeminiJsonClient
from .srt_utils import format_timestamp, parse_srt_timestamp


MAX_CHUNK_CUES = 120
MAX_CHUNK_CHARS = 16_000
NEIGHBOR_CUES = 3
MAX_PARALLEL_REQUESTS = 4
# Smooth mode never moves words across a pause this long, so cuts stay intact.
GROUP_GAP_SECONDS = 0.8
MAX_GROUP_CUES = 40
MAX_CUE_CHARS = 80
MIN_CUE_SECONDS = 0.3
# Guards against Gemini dropping or rewriting speech while re-splitting.
MIN_NEW_WORD_MATCH = 0.6
MIN_ORIGINAL_MATCH = 0.85
MIN_ORIGINAL_MATCH_FILLERS = 0.7
MAX_DROPPED_RUN = 6

TIMING_RE = re.compile(
    r"^\s*(\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{3}).*$"
)


@dataclass(frozen=True)
class SrtBlock:
    """One SRT cue kept verbatim except for its text."""

    position: int
    number: str
    timing: str
    text: str

    @property
    def start(self) -> float:
        match = TIMING_RE.match(self.timing)
        return parse_srt_timestamp(match.group(1)) if match else 0.0

    @property
    def end(self) -> float:
        match = TIMING_RE.match(self.timing)
        return parse_srt_timestamp(match.group(2)) if match else 0.0


@dataclass
class SrtChange:
    """A unit of the result: original cues and the cues that replace them."""

    before: tuple[SrtBlock, ...]
    after: tuple[SrtBlock, ...]
    note: str = ""

    @property
    def changed(self) -> bool:
        return [(b.timing, b.text) for b in self.before] != [(b.timing, b.text) for b in self.after]

    @property
    def label(self) -> str:
        first = self.before[0].number or str(self.before[0].position)
        last = self.before[-1].number or str(self.before[-1].position)
        return first if first == last else f"{first}-{last}"


def split_srt_and_notes(text: str) -> tuple[list[SrtBlock], str]:
    """Parse SRT cues; any pasted paragraph that is not a cue is returned as notes."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").strip()
    blocks: list[SrtBlock] = []
    notes: list[str] = []
    for raw in re.split(r"\n\s*\n", normalized) if normalized else []:
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = next(
            (index for index, line in enumerate(lines[:2]) if TIMING_RE.match(line)), None
        )
        if timing_index is None:
            notes.append("\n".join(lines))
            continue
        blocks.append(
            SrtBlock(
                position=len(blocks) + 1,
                number=lines[0] if timing_index == 1 else "",
                timing=lines[timing_index],
                text="\n".join(lines[timing_index + 1 :]),
            )
        )
    return blocks, "\n\n".join(notes)


def parse_srt_blocks(text: str) -> list[SrtBlock]:
    blocks, notes = split_srt_and_notes(text)
    if notes:
        raise ValueError(
            "Text is not valid SRT (missing 00:00:00,000 --> 00:00:00,000 line): "
            + notes.split("\n", 1)[0][:80]
        )
    return blocks


def format_srt_blocks(blocks: Sequence[SrtBlock], renumber: bool = False) -> str:
    return "\n".join(
        f"{index if renumber else (block.number or block.position)}\n{block.timing}\n{block.text}\n"
        for index, block in enumerate(blocks, start=1)
    )


def result_blocks(changes: Sequence[SrtChange]) -> list[SrtBlock]:
    return [block for change in changes for block in change.after]


def _timing(start: float, end: float) -> str:
    return f"{format_timestamp(start, True)} --> {format_timestamp(end, True)}"


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\\n", "\n").strip()
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def _cue_payload(blocks: Sequence[SrtBlock]) -> str:
    return json.dumps(
        [{"id": block.position, "text": block.text} for block in blocks], ensure_ascii=False
    )


def _chunks(units: Sequence[Sequence[SrtBlock]]) -> list[list[Sequence[SrtBlock]]]:
    """Pack units (single cues or groups) into requests of bounded size."""
    chunks: list[list[Sequence[SrtBlock]]] = []
    current: list[Sequence[SrtBlock]] = []
    cues = chars = 0
    for unit in units:
        size = sum(len(block.text) + 30 for block in unit)
        if current and (cues + len(unit) > MAX_CHUNK_CUES or chars + size > MAX_CHUNK_CHARS):
            chunks.append(current)
            current, cues, chars = [], 0, 0
        current.append(unit)
        cues += len(unit)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def _run_parallel(chunks: Sequence[Any], job: Callable[[int, Any], Any]) -> list[Any]:
    workers = max(1, min(MAX_PARALLEL_REQUESTS, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, index, chunk) for index, chunk in enumerate(chunks, start=1)]
        return [future.result() for future in futures]


def _style_rules(remove_fillers: bool) -> str:
    if remove_fillers:
        return (
            "- You MAY remove filler words, hesitations, stutters, and accidental\n"
            "  repetitions (e.g. \"ha\", \"ờ\", \"à\", \"thì ok thì\", \"um\", \"uh\") and\n"
            "  false starts. Do not remove any word that carries meaning."
        )
    return "- Keep every spoken word, including filler words."


def fix_srt(
    client: GeminiJsonClient,
    blocks: Sequence[SrtBlock],
    context: str,
    logger: Callable[[str], None],
    smooth: bool = False,
    remove_fillers: bool = False,
) -> list[SrtChange]:
    """Correct the SRT. Without smooth, only cue text changes and timing is untouched.

    With smooth, continuous speech is re-split at natural phrase boundaries and the
    new timestamps are derived from the original word timing.
    """
    if not any(block.text.strip() for block in blocks):
        raise ValueError("The SRT has no subtitle text to fix.")
    if smooth:
        return _smooth_srt(client, blocks, context, logger, remove_fillers)
    return _fix_text(client, blocks, context, logger, remove_fillers)


def _fix_text(
    client: GeminiJsonClient,
    blocks: Sequence[SrtBlock],
    context: str,
    logger: Callable[[str], None],
    remove_fillers: bool,
) -> list[SrtChange]:
    to_fix = [block for block in blocks if block.text.strip()]
    chunks = _chunks([[block] for block in to_fix])
    by_position = {block.position: index for index, block in enumerate(to_fix)}
    logger(f"Sending {len(to_fix)} cues to Gemini in {len(chunks)} request(s)...")

    def job(index: int, chunk: list[list[SrtBlock]]) -> list[tuple[int, str, str]]:
        cues = [unit[0] for unit in chunk]
        first = by_position[cues[0].position]
        last = by_position[cues[-1].position]
        before = to_fix[max(0, first - NEIGHBOR_CUES) : first]
        after = to_fix[last + 1 : last + 1 + NEIGHBOR_CUES]
        prompt = f"""
You are a meticulous subtitle proofreader. The SUBTITLES below came from speech
recognition (or were typed) and contain some mistakes: misheard or wrong words,
wrong names, brands, product or technical terms, wrong numbers, wrong spelling or
missing/wrong diacritics, and wrong punctuation.

Correct them using the REFERENCE / CONTEXT from the user. It may be the original
script, a sample of correctly written text, a glossary, or a description of the
topic, and it may also contain instructions from the user.

Rules:
- Fix recognition and spelling errors only. Keep the speaker's wording, language,
  and style. Do not translate, summarize, paraphrase, or censor.
{_style_rules(remove_fillers)}
- When the reference contains the exact words of a cue, match its wording and spelling.
- Never merge, split, reorder, add, or remove cues. Timing is not your concern.
- Keep each cue's line breaks ("\\n") unless a fix requires otherwise.
- Return only cues whose text actually changes, with the FULL corrected cue text.
- Use ONLY ids from CUES TO CORRECT. Neighbor cues are context and are never returned.
- note is a very short reason, in the language of the subtitles.
Return JSON only:
{{"fixes":[{{"id":12,"text":"full corrected cue text","note":"short reason"}}]}}
Return {{"fixes":[]}} when nothing needs fixing.

REFERENCE / CONTEXT FROM USER:
{context.strip() or "(none: fix only obvious recognition and spelling errors)"}

NEIGHBOR CUES BEFORE (context only):
{_cue_payload(before)}

CUES TO CORRECT (part {index}/{len(chunks)}):
{_cue_payload(cues)}

NEIGHBOR CUES AFTER (context only):
{_cue_payload(after)}
""".strip()
        payload = client.generate_json(
            prompt, f"fix SRT {index}/{len(chunks)}", max_output_tokens=32768
        )
        raw = payload.get("fixes") if isinstance(payload, dict) else payload
        valid_ids = {cue.position for cue in cues}
        fixes: list[tuple[int, str, str]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                position = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            text = _clean_text(item.get("text"))
            if position in valid_ids and text:
                fixes.append((position, text, str(item.get("note", "")).strip()))
        return fixes

    fixes: dict[int, tuple[str, str]] = {}
    for result in _run_parallel(chunks, job):
        for position, text, note in result:
            fixes.setdefault(position, (text, note))
    changes = []
    for block in blocks:
        text, note = fixes.get(block.position, (block.text, ""))
        changes.append(SrtChange((block,), (replace(block, text=text),), note))
    logger(f"Gemini corrected {sum(change.changed for change in changes)} of {len(to_fix)} cues.")
    return changes


def _speech_groups(blocks: Sequence[SrtBlock]) -> list[list[SrtBlock]]:
    """Continuous speech between pauses; words never move between groups."""
    groups: list[list[SrtBlock]] = []
    for block in blocks:
        current = groups[-1] if groups else None
        if (
            current is None
            or block.start - current[-1].end >= GROUP_GAP_SECONDS
            or block.start < current[-1].start - GROUP_GAP_SECONDS
            or len(current) >= MAX_GROUP_CUES
        ):
            groups.append([block])
        else:
            current.append(block)
    return groups


def _word_times(group: Sequence[SrtBlock]) -> tuple[list[str], list[tuple[float, float]]]:
    """Estimate each word's time by spreading the cue duration over its characters."""
    words: list[str] = []
    times: list[tuple[float, float]] = []
    for block in group:
        cue_words = block.text.split()
        total = sum(len(word) + 1 for word in cue_words) or 1
        cursor = block.start
        span = max(0.0, block.end - block.start)
        for word in cue_words:
            length = span * (len(word) + 1) / total
            words.append(word)
            times.append((cursor, cursor + length))
            cursor += length
    return words, times


def _normalize_word(word: str) -> str:
    return re.sub(r"[^\w]+", "", word.lower())


def _retime_group(
    group: Sequence[SrtBlock], new_texts: Sequence[str], remove_fillers: bool = False
) -> tuple[list[SrtBlock] | None, str]:
    """Give each new cue the timing of the original words it contains."""
    original_words, times = _word_times(group)
    new_cues = [(text, text.split()) for text in new_texts if text.split()]
    new_words = [word for _, words in new_cues for word in words]
    if not original_words or not new_words:
        return None, "Gemini returned no text for this group"
    matcher = difflib.SequenceMatcher(
        None,
        [_normalize_word(word) for word in original_words],
        [_normalize_word(word) for word in new_words],
        autojunk=False,
    )
    mapped: list[int | None] = [None] * len(new_words)
    matched = 0
    previous_end = (0, 0)
    for block in matcher.get_matching_blocks():
        dropped = (block.a - previous_end[0]) - (block.b - previous_end[1])
        if dropped >= MAX_DROPPED_RUN:
            return None, f"Gemini dropped {dropped} words in a row; kept the original cues"
        previous_end = (block.a + block.size, block.b + block.size)
        for offset in range(block.size):
            mapped[block.b + offset] = block.a + offset
        matched += block.size
    minimum_original = MIN_ORIGINAL_MATCH_FILLERS if remove_fillers else MIN_ORIGINAL_MATCH
    if (
        matched < MIN_NEW_WORD_MATCH * len(new_words)
        or matched < minimum_original * len(original_words)
    ):
        return None, "Gemini changed the wording too much; kept the original cues"

    previous: list[int | None] = []
    last: int | None = None
    for value in mapped:
        last = value if value is not None else last
        previous.append(last)
    following: list[int | None] = [None] * len(mapped)
    upcoming: int | None = None
    for index in range(len(mapped) - 1, -1, -1):
        upcoming = mapped[index] if mapped[index] is not None else upcoming
        following[index] = upcoming

    def anchor(index: int, prefer_next: bool) -> int:
        order = (mapped[index], following[index], previous[index]) if prefer_next else (
            mapped[index], previous[index], following[index]
        )
        return next(value for value in order if value is not None)

    spans: list[list[float]] = []
    cursor = 0
    for _, words in new_cues:
        first = anchor(cursor, prefer_next=True)
        final = max(first, anchor(cursor + len(words) - 1, prefer_next=False))
        spans.append([times[first][0], times[final][1]])
        cursor += len(words)
    spans[0][0] = group[0].start
    spans[-1][1] = max(spans[-1][0] + MIN_CUE_SECONDS, group[-1].end)
    for index in range(1, len(spans)):
        spans[index][0] = max(spans[index][0], spans[index - 1][0] + MIN_CUE_SECONDS)
        spans[index - 1][1] = spans[index][0]
    for span in spans:
        span[1] = max(span[1], span[0] + MIN_CUE_SECONDS)

    blocks = [
        SrtBlock(group[0].position, "", _timing(start, end), text)
        for (text, _), (start, end) in zip(new_cues, spans)
    ]
    return blocks, ""


def _smooth_srt(
    client: GeminiJsonClient,
    blocks: Sequence[SrtBlock],
    context: str,
    logger: Callable[[str], None],
    remove_fillers: bool,
) -> list[SrtChange]:
    groups = _speech_groups([block for block in blocks if block.text.strip()])
    chunks = _chunks(groups)
    group_ids = {id(group): index for index, group in enumerate(groups, start=1)}
    logger(
        f"Smoothing {len(groups)} speech group(s) in {len(chunks)} request(s)..."
    )

    def group_payload(group: Sequence[SrtBlock]) -> dict[str, Any]:
        return {
            "group": group_ids[id(group)],
            "durationSeconds": round(group[-1].end - group[0].start, 2),
            "text": " ".join(block.text.replace("\n", " ") for block in group),
        }

    def job(index: int, chunk: list[list[SrtBlock]]) -> dict[int, list[str]]:
        first = group_ids[id(chunk[0])] - 1
        last = group_ids[id(chunk[-1])] - 1
        before = [group_payload(group)["text"] for group in groups[max(0, first - 1) : first]]
        after = [group_payload(group)["text"] for group in groups[last + 1 : last + 2]]
        prompt = f"""
You are a professional subtitle editor. Each GROUP below is continuous speech from a
video (groups are separated by pauses). The current subtitle cues were cut at fixed
lengths, so they break in the middle of phrases and read choppily. Re-split each
group's speech into cues that read smoothly.

Rules:
- Break cues at natural sentence or phrase boundaries, never in the middle of a
  phrase, name, number, or term.
- Each cue is one sentence or a natural clause: ideally 2-7 seconds of speech and at
  most about {MAX_CUE_CHARS} characters. Split long sentences at clauses or commas.
  Avoid cues of only 1-2 words unless they are a complete short sentence.
- Add natural punctuation and capitalization.
- Keep the words in the same order. Do not paraphrase, summarize, translate, or add
  content.
{_style_rules(remove_fillers)}
- Also fix misheard words, names, brands, terms, numbers, spelling, and diacritics
  using the REFERENCE / CONTEXT. When the reference has the exact words, match them.
- Words must stay inside their own group; never move words to another group.
- Return every group listed, using the same group numbers.
Return JSON only:
{{"groups":[{{"group":1,"cues":["First natural sentence.","Second phrase, ..."]}}]}}

REFERENCE / CONTEXT FROM USER:
{context.strip() or "(none: fix only obvious recognition and spelling errors)"}

PREVIOUS SPEECH (context only): {json.dumps(before, ensure_ascii=False)}

GROUPS TO RE-SPLIT (part {index}/{len(chunks)}):
{json.dumps([group_payload(group) for group in chunk], ensure_ascii=False)}

NEXT SPEECH (context only): {json.dumps(after, ensure_ascii=False)}
""".strip()
        payload = client.generate_json(
            prompt, f"smooth SRT {index}/{len(chunks)}", max_output_tokens=32768
        )
        raw = payload.get("groups") if isinstance(payload, dict) else payload
        valid = {group_ids[id(group)] for group in chunk}
        result: dict[int, list[str]] = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("cues"), list):
                continue
            try:
                number = int(item.get("group"))
            except (TypeError, ValueError):
                continue
            texts = [" ".join(_clean_text(text).split()) for text in item["cues"]]
            if number in valid and any(texts):
                result.setdefault(number, [text for text in texts if text])
        return result

    answers: dict[int, list[str]] = {}
    for result in _run_parallel(chunks, job):
        answers.update(result)

    changes: list[SrtChange] = []
    kept = 0
    for number, group in enumerate(groups, start=1):
        new_blocks, problem = (
            _retime_group(group, answers[number], remove_fillers)
            if number in answers
            else (None, "Gemini skipped this group; kept the original cues")
        )
        if new_blocks is None:
            kept += 1
            logger(f"Group {number} (cues {group[0].position}-{group[-1].position}): {problem}.")
            changes.append(SrtChange(tuple(group), tuple(group), problem))
        else:
            changes.append(SrtChange(tuple(group), tuple(new_blocks)))
    changed = sum(change.changed for change in changes)
    logger(
        f"Smoothed {changed} of {len(groups)} group(s): "
        f"{len(result_blocks(changes))} cues (was "
        f"{sum(len(group) for group in groups)})."
        + (f" {kept} group(s) kept as original." if kept else "")
    )
    return changes
