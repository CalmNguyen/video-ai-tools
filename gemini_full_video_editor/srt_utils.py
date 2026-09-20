from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from .models import Cue


def _mojibake_score(text: str) -> int:
    markers = ("Ã", "Â", "Ä", "áº", "á»", "â€", "ðŸ", "ï»¿", "�")
    return sum(text.count(marker) for marker in markers)


def repair_mojibake(text: str) -> tuple[str, bool]:
    current = text.lstrip("\ufeff")
    changed = False
    for _ in range(2):
        if _mojibake_score(current) == 0:
            break
        candidates: list[str] = []
        for mistaken_encoding in ("cp1252", "latin1"):
            try:
                candidates.append(current.encode(mistaken_encoding).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if not candidates:
            break
        candidate = min(candidates, key=_mojibake_score)
        if _mojibake_score(candidate) >= _mojibake_score(current):
            break
        current = candidate
        changed = True
    return current, changed


def read_text_file(path: str | Path) -> tuple[str, str, bool]:
    raw = Path(path).read_bytes()
    encodings = ["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig"]
    encodings.extend(("cp1258", "cp1252"))
    decoded = ""
    encoding = ""
    for candidate in encodings:
        try:
            decoded = raw.decode(candidate)
        except (UnicodeDecodeError, UnicodeError):
            continue
        encoding = candidate
        break
    if not encoding:
        decoded = raw.decode("utf-8", errors="replace")
        encoding = "utf-8 (replace)"
    repaired, changed = repair_mojibake(decoded)
    return repaired, encoding, changed


def parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def format_timestamp(seconds: float, srt: bool = False) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


def parse_srt(text: str) -> list[Cue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues: list[Cue] = []
    generated_id = 1
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        has_index = bool(re.fullmatch(r"\d+", lines[0]))
        timing_index = 1 if has_index else 0
        if timing_index >= len(lines):
            continue
        match = re.match(
            r"(\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d+:\d{2}:\d{2}[,.]\d{3})",
            lines[timing_index],
        )
        if not match:
            continue
        start = parse_srt_timestamp(match.group(1))
        end = parse_srt_timestamp(match.group(2))
        caption = " ".join(lines[timing_index + 1 :]).strip()
        if not caption or end <= start:
            continue
        cue_id = int(lines[0]) if has_index else generated_id
        if any(cue.id == cue_id for cue in cues):
            cue_id = max((cue.id for cue in cues), default=0) + 1
        cues.append(Cue(id=cue_id, start=start, end=end, text=caption))
        generated_id = max(generated_id + 1, cue_id + 1)
    cues.sort(key=lambda cue: (cue.start, cue.end, cue.id))
    return cues


def read_srt(path: str | Path) -> tuple[list[Cue], str, bool]:
    text, encoding, repaired = read_text_file(path)
    return parse_srt(text), encoding, repaired


def transcript_for_cues(cues: Sequence[Cue]) -> str:
    return "\n".join(
        f"[ID {cue.id} | {format_timestamp(cue.start)} --> "
        f"{format_timestamp(cue.end)}] {cue.text}"
        for cue in cues
    )


def cue_dicts(cues: Sequence[Cue]) -> list[dict[str, object]]:
    return [cue.to_dict() for cue in cues]


def write_srt(cues: Sequence[Cue], path: Path) -> None:
    lines: list[str] = []
    for index, cue in enumerate(cues, start=1):
        lines.extend(
            [
                str(index),
                f"{format_timestamp(cue.start, True)} --> {format_timestamp(cue.end, True)}",
                cue.text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8-sig")
