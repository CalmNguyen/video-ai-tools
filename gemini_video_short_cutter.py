"""
Gemini Video Short Cutter - desktop tool for Windows (Python 3.11+).

Install Python dependency:
    pip install -U google-genai

FFmpeg and ffprobe must also be available in PATH.

Workflow:
1. Read one or many MP4 files. A missing SRT is created automatically by
   Gemini (API key A / model A, e.g. a free key + flash-lite).
2. Ask Gemini (API key B / model B, e.g. a paid key + a stronger model) to find the strongest self-contained short clips.
3. Call Gemini once per selected clip to create title, description, and a
   thumbnail-generation prompt (small independent requests are more reliable).
4. Use FFmpeg to cut each clip and convert it to 1080x1920 vertical video.
5. Save MP4, clipped SRT, per-short metadata TXT, and a complete plan JSON.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


def _load_stdlib_copy_before_local_modules() -> None:
    """Prevent a neighboring copy.py from shadowing Python's stdlib module."""
    existing = sys.modules.get("copy")
    existing_file = getattr(existing, "__file__", "") if existing else ""
    local_copy = Path(__file__).resolve().with_name("copy.py")
    if existing and (not existing_file or Path(existing_file).resolve() != local_copy):
        return
    if existing:
        sys.modules.pop("copy", None)
    original_path = list(sys.path)
    script_dir = Path(__file__).resolve().parent
    try:
        sys.path[:] = [
            entry
            for entry in original_path
            if Path(entry or os.getcwd()).resolve() != script_dir
        ]
        __import__("copy")
    finally:
        sys.path[:] = original_path


_load_stdlib_copy_before_local_modules()

try:
    from google import genai
    from google.genai import types
except ImportError:  # handled with a friendly message at runtime
    genai = None
    types = None


from gemini_full_video_editor.gemini_audio import transcribe_media  # noqa: E402
from gemini_full_video_editor.gemini_client import GeminiJsonClient  # noqa: E402
from gemini_full_video_editor.srt_utils import write_srt as write_generated_srt  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = SCRIPT_DIR / "gemini_video_short_cutter.settings.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "short_outputs"
DEFAULT_MODELS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)
RETRY_DELAYS = (5, 15, 30)
DEFAULT_API_CONCURRENCY = 3
DEFAULT_VIDEO_PARALLEL = 2
MAX_VIDEO_PARALLEL = 6
DEFAULT_SRT_PARALLEL = 2
DEFAULT_SRT_MODEL = "gemini-3.5-flash-lite"
MAX_API_CONCURRENCY = 8
ANALYSIS_CHUNK_SECONDS = 15 * 60
ANALYSIS_CHUNK_MAX_CHARS = 50_000
ANALYSIS_OVERLAP_SECONDS = 45
VERTICAL_BLUR = "Giữ toàn cảnh + nền mờ"
VERTICAL_CROP = "Crop giữa toàn màn hình"
VERTICAL_MODES = (VERTICAL_BLUR, VERTICAL_CROP)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass
class ShortPlan:
    start: float
    end: float
    topic: str
    reason: str
    score: float
    title: str = ""
    description: str = ""
    thumbnail_prompt: str = ""
    video_path: str = ""
    subtitle_path: str = ""
    metadata_path: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class RunConfig:
    api_key: str
    model: str
    srt_api_key: str
    srt_model: str
    auto_srt: bool
    video_paths: tuple[Path, ...]
    video_parallel: int
    srt_parallel: int
    output_dir: Path
    min_duration: int
    max_duration: int
    max_shorts: int
    api_concurrency: int
    vertical_mode: str
    burn_subtitles: bool
    extra_instructions: str


def ensure_dependencies() -> None:
    if genai is None or types is None:
        raise RuntimeError(
            "Thiếu package google-genai. Hãy chạy: pip install -U google-genai"
        )
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError(
            "Không tìm thấy " + ", ".join(missing) + ". Hãy cài FFmpeg và thêm vào PATH."
        )


def parse_number(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} phải là số nguyên.") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field_name} phải nằm trong khoảng {minimum}-{maximum}.")
    return parsed


def format_timestamp(seconds: float, srt: bool = False) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


def parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(
        r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value.strip()
    )
    if not match:
        raise ValueError(f"Timestamp SRT không hợp lệ: {value}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _mojibake_score(text: str) -> int:
    markers = ("Ã", "Â", "Ä", "áº", "á»", "â€", "ðŸ", "ï»¿", "�")
    return sum(text.count(marker) for marker in markers)


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Repair UTF-8 bytes mistakenly decoded as Windows-1252/Latin-1."""
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


def read_srt_text(path: str | Path) -> tuple[str, str, bool]:
    raw = Path(path).read_bytes()
    # Prefer a valid UTF encoding deterministically. Single-byte encodings can
    # decode nearly any byte sequence, so scoring them alongside UTF-8 would
    # sometimes select readable-looking garbage.
    encodings = ["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig"]
    encodings.extend(("cp1258", "cp1252"))
    decoded = ""
    encoding = ""
    for candidate_encoding in encodings:
        try:
            decoded = raw.decode(candidate_encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        encoding = candidate_encoding
        break
    if not encoding:
        decoded = raw.decode("utf-8", errors="replace")
        encoding = "utf-8 (replace)"
    repaired, changed = repair_mojibake(decoded)
    return repaired, encoding, changed


def parse_srt(text: str) -> list[Cue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = 1 if re.fullmatch(r"\d+", lines[0]) else 0
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
        if caption and end > start:
            cues.append(Cue(start=start, end=end, text=caption))
    cues.sort(key=lambda cue: (cue.start, cue.end))
    return cues


def video_srt_path(video: Path) -> Path:
    return video.with_suffix(".srt")


def probe_duration(path: str | Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe không đọc được video.")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Không đọc được thời lượng video từ ffprobe.") from exc


def transcript_for_cues(cues: Sequence[Cue]) -> str:
    return "\n".join(
        f"[{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}] {cue.text}"
        for cue in cues
    )


def cues_for_range(cues: Sequence[Cue], start: float, end: float) -> list[Cue]:
    return [cue for cue in cues if cue.end > start and cue.start < end]


def chunk_cues(cues: Sequence[Cue]) -> list[list[Cue]]:
    if not cues:
        return []
    chunks: list[list[Cue]] = []
    start_index = 0
    while start_index < len(cues):
        chunk_start = cues[start_index].start
        char_count = 0
        end_index = start_index
        while end_index < len(cues):
            cue = cues[end_index]
            next_chars = char_count + len(cue.text) + 45
            too_long = cue.end - chunk_start > ANALYSIS_CHUNK_SECONDS
            too_large = next_chars > ANALYSIS_CHUNK_MAX_CHARS
            if end_index > start_index and (too_long or too_large):
                break
            char_count = next_chars
            end_index += 1
        chunks.append(list(cues[start_index:end_index]))
        if end_index >= len(cues):
            break
        overlap_from = cues[end_index - 1].end - ANALYSIS_OVERLAP_SECONDS
        next_index = end_index
        for index in range(end_index - 1, start_index, -1):
            if cues[index].start <= overlap_from:
                next_index = index
                break
        if next_index <= start_index:
            next_index = end_index
        start_index = next_index
    return chunks


def extract_json(raw_text: str) -> Any:
    text = (raw_text or "").strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(text) if char in "[{"]
    for start in starts:
        try:
            payload, _ = decoder.raw_decode(text[start:])
            return payload
        except json.JSONDecodeError:
            continue
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        payload = ast.literal_eval(repaired)
        if isinstance(payload, (dict, list)):
            return payload
    except Exception:
        pass
    raise RuntimeError("Gemini trả về JSON không hợp lệ.")


def response_text(response: Any) -> str:
    direct = getattr(response, "text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            value = getattr(part, "text", None)
            if value:
                parts.append(str(value))
    if parts:
        return "\n".join(parts)
    raise RuntimeError("Gemini không trả về nội dung text.")


def retryable_gemini_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429", "rate limit", "quota", "resource exhausted", "500", "502",
            "503", "504", "timeout", "temporarily unavailable", "connection",
        )
    )


class GeminiShortClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        logger: Callable[[str], None],
        cancel_event: threading.Event | None = None,
    ):
        ensure_dependencies()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.logger = logger
        self.cancel_event = cancel_event

    def _generate_json(self, prompt: str, label: str) -> Any:
        last_error: Exception | None = None
        # Mỗi worker có client riêng để các request đồng thời không dùng chung
        # connection state của SDK. Client được tái sử dụng cho các lần retry.
        with genai.Client(api_key=self.api_key) as client:
            for attempt in range(1, len(RETRY_DELAYS) + 2):
                if self.cancel_event and self.cancel_event.is_set():
                    raise InterruptedError("Đã dừng theo yêu cầu.")
                try:
                    self.logger(f"Gemini [{label}] - lần gọi {attempt}...")
                    response = client.models.generate_content(
                        model=self.model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            max_output_tokens=8192,
                        ),
                    )
                    return extract_json(response_text(response))
                except InterruptedError:
                    raise
                except Exception as exc:
                    last_error = exc
                    can_retry = attempt <= len(RETRY_DELAYS)
                    retry = retryable_gemini_error(exc) or "JSON" in str(exc)
                    if not can_retry or not retry:
                        raise
                    delay = RETRY_DELAYS[attempt - 1]
                    self.logger(f"Gemini lỗi: {exc}. Thử lại sau {delay} giây...")
                    if self.cancel_event:
                        if self.cancel_event.wait(delay):
                            raise InterruptedError("Đã dừng theo yêu cầu.")
                    else:
                        time.sleep(delay)
        raise RuntimeError(str(last_error) if last_error else "Gemini request failed")

    def find_candidates(
        self,
        cues: Sequence[Cue],
        min_duration: int,
        max_duration: int,
        candidate_limit: int,
        extra_instructions: str,
        chunk_index: int,
        chunk_count: int,
    ) -> Any:
        transcript = transcript_for_cues(cues)
        extra = extra_instructions.strip() or "(Không có yêu cầu bổ sung.)"
        prompt = f"""
Bạn là biên tập viên video short chuyên nghiệp. Hãy tìm tối đa {candidate_limit}
đoạn hay nhất, tự đứng độc lập và có khả năng giữ chân người xem trong phần
transcript {chunk_index}/{chunk_count} dưới đây.

Yêu cầu bắt buộc:
- Mỗi đoạn dài từ {min_duration} đến {max_duration} giây.
- start_seconds và end_seconds là timestamp TUYỆT ĐỐI từ transcript, phải bắt đầu
  và kết thúc tại ranh giới câu/subtitle hợp lý.
- Ưu tiên đoạn có hook, giá trị rõ ràng, cao trào, mẹo hữu ích hoặc quan điểm đáng chú ý.
- Không chọn đoạn phụ thuộc quá nhiều vào nội dung trước/sau; tránh lời chào, intro,
  outro, quảng cáo lan man và các đoạn trùng ý.
- Không bịa nội dung ngoài transcript.
- score là số 0-100 thể hiện tiềm năng làm short.
- Các trường topic và reason phải dùng cùng ngôn ngữ chính với transcript.
- Không dịch nội dung sang ngôn ngữ khác. Nếu transcript có nhiều ngôn ngữ, dùng
  ngôn ngữ được người nói sử dụng nhiều nhất.
- Chỉ trả JSON hợp lệ theo đúng dạng:
{{"shorts":[{{"start_seconds":0.0,"end_seconds":45.0,"topic":"...",
"reason":"...","score":90}}]}}

Yêu cầu bổ sung của người dùng:
{extra}

TRANSCRIPT:
{transcript}
""".strip()
        return self._generate_json(prompt, f"phân tích {chunk_index}/{chunk_count}")

    def create_metadata(
        self, plan: ShortPlan, cues: Sequence[Cue], index: int, video_name: str = ""
    ) -> dict[str, str]:
        transcript = transcript_for_cues(cues)
        prompt = f"""
Bạn là chuyên gia nội dung video short và là art director chuyên thiết kế thumbnail
có tỷ lệ nhấp cao. Dựa CHỈ trên đoạn transcript bên dưới, hãy viết metadata hoàn chỉnh.

QUY TẮC NGÔN NGỮ (áp dụng cho title và description):
- Tự xác định ngôn ngữ người nói dùng chủ yếu trong transcript và viết title,
  description, hashtag bằng đúng ngôn ngữ đó; không dịch sang ngôn ngữ khác.
- Giữ nguyên tên riêng, tên sản phẩm và thuật ngữ chuyên môn khi cần thiết.

- title: hấp dẫn, cụ thể, tự nhiên, tối đa 100 ký tự; không clickbait sai sự thật.
- description: 2-4 câu dễ đăng lên YouTube Shorts/TikTok, sau đó 3-6 hashtag phù hợp.
- thumbnail_prompt: viết bằng TIẾNG ANH (model tạo ảnh hiểu tiếng Anh tốt nhất),
  một đoạn văn liền mạch 80-140 từ, dùng cho ảnh dọc 9:16 (1080x1920). Bắt buộc có đủ:
  1) Chủ thể chính MỘT điểm nhấn duy nhất, mô tả cụ thể (người/vật/cảnh liên quan
     trực tiếp tới khoảnh khắc hay nhất của đoạn này, không chung chung);
  2) Biểu cảm/cảm xúc mạnh và hành động (curiosity, shock, joy, tension...);
  3) Khung hình: cận cảnh/trung cảnh, góc máy, chủ thể chiếm khoảng 60% khung hình
     và đặt ở khoảng giữa; chừa vùng trống ở 15% trên cùng và 20% dưới cùng
     (vùng giao diện app che) để thêm chữ sau;
  4) Bối cảnh đơn giản, phông nền tách biệt khỏi chủ thể (shallow depth of field);
  5) Ánh sáng (rim light/soft key light), bảng màu tương phản cao, 2-3 màu chủ đạo, bão hòa;
  6) Phong cách (photorealistic hoặc cinematic hoặc 3D render... phù hợp nội dung);
  7) Hình phải đọc rõ khi thu nhỏ bằng ngón tay cái. Đặt câu "no text, no watermark,
     no logo, no captions" ở cuối để chữ được thêm bằng công cụ khác.
  Không nhắc tên người thật hay thương hiệu có bản quyền trong prompt.
- Không thêm markdown. Chỉ trả JSON hợp lệ đúng dạng:
{{"title":"...","description":"...","thumbnail_prompt":"..."}}

Video gốc: {video_name}
Chủ đề dự kiến: {plan.topic}
Lý do chọn đoạn này: {plan.reason or "(không có)"}
Thời lượng: {plan.duration:.1f} giây

TRANSCRIPT CỦA SHORT {index}:
{transcript}
""".strip()
        payload = self._generate_json(prompt, f"metadata short {index}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Metadata short {index} không phải JSON object.")
        result = {
            "title": str(payload.get("title", "")).strip(),
            "description": str(payload.get("description", "")).strip(),
            "thumbnail_prompt": str(
                payload.get("thumbnail_prompt", payload.get("thumbnailPrompt", ""))
            ).strip(),
        }
        if not all(result.values()):
            raise RuntimeError(f"Metadata short {index} thiếu title/description/thumbnail_prompt.")
        return result


def run_parallel_api_jobs(
    jobs: Sequence[Callable[[], Any]],
    max_workers: int,
    cancel_event: threading.Event,
) -> list[Any]:
    """Run independent Gemini jobs concurrently and preserve input order."""
    if not jobs:
        return []
    worker_count = max(1, min(max_workers, len(jobs)))
    if worker_count == 1:
        results: list[Any] = []
        for job in jobs:
            if cancel_event.is_set():
                raise InterruptedError("Đã dừng theo yêu cầu.")
            results.append(job())
        return results

    results = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="gemini-api") as executor:
        futures = {executor.submit(job): index for index, job in enumerate(jobs)}
        try:
            for future in as_completed(futures):
                if cancel_event.is_set():
                    raise InterruptedError("Đã dừng theo yêu cầu.")
                results[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return results


def _float_value(item: dict[str, Any], names: Sequence[str], default: float = 0.0) -> float:
    for name in names:
        if name in item:
            try:
                return float(item[name])
            except (TypeError, ValueError):
                pass
    return default


def _nearest(values: Sequence[float], target: float) -> float:
    return min(values, key=lambda value: abs(value - target))


def normalize_candidates(
    payload: Any,
    source_cues: Sequence[Cue],
    min_duration: int,
    max_duration: int,
    video_duration: float,
) -> list[ShortPlan]:
    items = payload.get("shorts", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not source_cues:
        return []
    starts = [cue.start for cue in source_cues]
    ends = [cue.end for cue in source_cues]
    result: list[ShortPlan] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_start = _float_value(item, ("start_seconds", "start", "startSeconds"), -1)
        raw_end = _float_value(item, ("end_seconds", "end", "endSeconds"), -1)
        if raw_start < 0 or raw_end <= raw_start:
            continue
        start = max(0.0, _nearest(starts, raw_start))
        end = min(video_duration, _nearest(ends, raw_end))
        if end <= start:
            continue
        if end - start > max_duration:
            valid_ends = [value for value in ends if start + min_duration <= value <= start + max_duration]
            if not valid_ends:
                continue
            end = max(valid_ends)
        elif end - start < min_duration:
            valid_ends = [value for value in ends if start + min_duration <= value <= start + max_duration]
            if not valid_ends:
                continue
            end = min(valid_ends)
        if end > video_duration + 0.05 or not min_duration <= end - start <= max_duration + 0.05:
            continue
        result.append(
            ShortPlan(
                start=round(start, 3),
                end=round(end, 3),
                topic=str(item.get("topic", "Đoạn nổi bật")).strip() or "Đoạn nổi bật",
                reason=str(item.get("reason", "")).strip(),
                score=max(0.0, min(100.0, _float_value(item, ("score",), 50))),
            )
        )
    return result


def select_best_candidates(candidates: Sequence[ShortPlan], limit: int) -> list[ShortPlan]:
    selected: list[ShortPlan] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        duplicate = False
        for existing in selected:
            overlap = max(0.0, min(candidate.end, existing.end) - max(candidate.start, existing.start))
            if overlap / max(0.001, min(candidate.duration, existing.duration)) >= 0.35:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda item: item.start)


def write_clipped_srt(cues: Sequence[Cue], start: float, end: float, path: Path) -> None:
    lines: list[str] = []
    index = 1
    for cue in cues:
        if cue.end <= start or cue.start >= end:
            continue
        local_start = max(cue.start, start) - start
        local_end = min(cue.end, end) - start
        if local_end <= local_start:
            continue
        lines.extend(
            [
                str(index),
                f"{format_timestamp(local_start, True)} --> {format_timestamp(local_end, True)}",
                cue.text,
                "",
            ]
        )
        index += 1
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def safe_filename(value: str, fallback: str, max_length: int = 70) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    cleaned = re.sub(r"\s+", "_", cleaned.strip()).strip(". _")
    return (cleaned[:max_length].rstrip(". _") or fallback)


def escape_subtitle_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace("[", r"\[").replace("]", r"\]")
    return value


def build_video_filter(vertical_mode: str, subtitle_path: Path | None) -> str:
    if vertical_mode == VERTICAL_CROP:
        base = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1[base]"
        )
    else:
        base = (
            "[0:v]split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=luma_radius=35:luma_power=1[bg];"
            "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
    if subtitle_path is None:
        return base + ";[base]null[vout]"
    escaped = escape_subtitle_filter_path(subtitle_path)
    style = (
        "FontName=Arial,FontSize=11,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=0,"
        # libass scales MarginV with its subtitle canvas. A value around 70
        # places two subtitle lines just below a centered 16:9 source frame in
        # the 9:16 blurred-background layout, instead of near the phone UI.
        "Alignment=2,MarginV=70,MarginL=18,MarginR=18"
    )
    return base + f";[base]subtitles=filename='{escaped}':force_style='{style}'[vout]"


def run_ffmpeg(
    video_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    vertical_mode: str,
    subtitle_path: Path | None,
    logger: Callable[[str], None],
    cancel_event: threading.Event,
) -> None:
    video_filter = build_video_filter(vertical_mode, subtitle_path)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(video_path), "-t", f"{duration:.3f}",
        "-filter_complex", video_filter,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(output_path),
    ]
    logger("FFmpeg: " + " ".join(command[:10]) + " ...")
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    while process.poll() is None:
        if cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            raise InterruptedError("Đã dừng theo yêu cầu.")
        time.sleep(0.2)
    stderr = process.stderr.read() if process.stderr else ""
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg thất bại ({process.returncode}): {stderr.strip()}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg không tạo được file output.")


def write_metadata(path: Path, plan: ShortPlan, index: int) -> None:
    content = (
        f"SHORT {index:02d}\n"
        f"Time: {format_timestamp(plan.start)} --> {format_timestamp(plan.end)} "
        f"({plan.duration:.1f}s)\n\n"
        f"TITLE\n{plan.title}\n\n"
        f"DESCRIPTION\n{plan.description}\n\n"
        f"THUMBNAIL PROMPT\n{plan.thumbnail_prompt}\n"
    )
    path.write_text(content, encoding="utf-8-sig")


def build_run_directory(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = root / datetime.now().strftime("shorts_%Y%m%d_%H%M%S")
    candidate = base
    number = 2
    while candidate.exists():
        candidate = Path(f"{base}_{number}")
        number += 1
    candidate.mkdir(parents=True)
    return candidate


class ShortCutterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Gemini Video Short Cutter")
        self.root.geometry("1040x850")
        self.root.minsize(900, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()

        self.api_key_var = tk.StringVar(value=os.getenv("GEMINI_API_KEY", "").strip())
        self.remember_key_var = tk.BooleanVar(value=False)
        self.model_var = tk.StringVar(value=DEFAULT_MODELS[0])
        self.srt_api_key_var = tk.StringVar(value=os.getenv("GEMINI_API_KEY", "").strip())
        self.srt_model_var = tk.StringVar(value=DEFAULT_SRT_MODEL)
        self.auto_srt_var = tk.BooleanVar(value=True)
        self.video_parallel_var = tk.StringVar(value=str(DEFAULT_VIDEO_PARALLEL))
        self.srt_parallel_var = tk.StringVar(value=str(DEFAULT_SRT_PARALLEL))
        self.video_paths: list[str] = []
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.min_duration_var = tk.StringVar(value="25")
        self.max_duration_var = tk.StringVar(value="60")
        self.max_shorts_var = tk.StringVar(value="10")
        self.api_concurrency_var = tk.StringVar(value=str(DEFAULT_API_CONCURRENCY))
        self.vertical_mode_var = tk.StringVar(value=VERTICAL_BLUR)
        self.burn_subtitles_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Sẵn sàng")
        self.last_output_var = tk.StringVar()

        self._build_ui()
        self._load_settings()
        self.root.after(120, self._flush_events)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(7, weight=1)

        srt_box = ttk.LabelFrame(
            main, text="Gemini A - tự lấy SRT từ video (nên dùng key free + flash-lite)", padding=8
        )
        srt_box.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        srt_box.columnconfigure(1, weight=1)
        ttk.Label(srt_box, text="API Key A").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(srt_box, textvariable=self.srt_api_key_var, show="*").grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Checkbutton(srt_box, text="Lưu key trên máy", variable=self.remember_key_var).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Label(srt_box, text="Model A").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(7, 0))
        ttk.Combobox(
            srt_box, textvariable=self.srt_model_var, values=DEFAULT_MODELS, state="normal"
        ).grid(row=1, column=1, sticky="ew", pady=(7, 0))
        ttk.Checkbutton(
            srt_box, text="Tự lấy SRT khi video chưa có file .srt cùng tên", variable=self.auto_srt_var
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(7, 0))

        api = ttk.LabelFrame(
            main,
            text="Gemini B - lên plan + title/description/thumbnail (nên dùng key paid + model mạnh)",
            padding=8,
        )
        api.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        api.columnconfigure(1, weight=1)
        ttk.Label(api, text="API Key B").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(api, textvariable=self.api_key_var, show="*").grid(row=0, column=1, sticky="ew")
        ttk.Label(api, text="Model B").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(7, 0))
        ttk.Combobox(
            api, textvariable=self.model_var, values=DEFAULT_MODELS, state="normal"
        ).grid(row=1, column=1, sticky="ew", pady=(7, 0))
        ttk.Label(api, text="Có thể chọn hoặc nhập model ID khác").grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(7, 0)
        )
        ttk.Label(api, text="Request API B song song").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(7, 0)
        )
        ttk.Spinbox(
            api, from_=1, to=MAX_API_CONCURRENCY, width=7, textvariable=self.api_concurrency_var
        ).grid(row=2, column=1, sticky="w", pady=(7, 0))
        ttk.Label(api, text="Khuyên dùng 2-4 để tránh rate limit").grid(
            row=2, column=2, sticky="w", padx=(8, 0), pady=(7, 0)
        )

        files = ttk.LabelFrame(main, text="Video đầu vào (chọn nhiều file) / đầu ra", padding=8)
        files.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        files.columnconfigure(0, weight=1)
        list_frame = ttk.Frame(files)
        list_frame.grid(row=0, column=0, sticky="ew")
        list_frame.columnconfigure(0, weight=1)
        self.video_list = tk.Listbox(list_frame, height=5, selectmode="extended")
        self.video_list.grid(row=0, column=0, sticky="ew")
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.video_list.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.video_list.configure(yscrollcommand=list_scroll.set)
        buttons = ttk.Frame(files)
        buttons.grid(row=0, column=1, sticky="n", padx=(8, 0))
        ttk.Button(buttons, text="Thêm video...", command=self._choose_videos).pack(fill="x")
        ttk.Button(buttons, text="Xóa đã chọn", command=self._remove_videos).pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="Xóa hết", command=self._clear_videos).pack(fill="x", pady=(4, 0))
        out_row = ttk.Frame(files)
        out_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        out_row.columnconfigure(1, weight=1)
        self._file_row(out_row, 0, "Thư mục output", self.output_dir_var, self._choose_output)
        parallel = ttk.Frame(files)
        parallel.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(parallel, text="Số video chạy song song").pack(side="left")
        ttk.Spinbox(
            parallel, from_=1, to=MAX_VIDEO_PARALLEL, width=5, textvariable=self.video_parallel_var
        ).pack(side="left", padx=(6, 18))
        ttk.Label(parallel, text="Video lấy SRT cùng lúc (key free nên để 1-2)").pack(side="left")
        ttk.Spinbox(
            parallel, from_=1, to=MAX_VIDEO_PARALLEL, width=5, textvariable=self.srt_parallel_var
        ).pack(side="left", padx=(6, 0))

        options = ttk.LabelFrame(main, text="Cấu hình short", padding=8)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(options, text="Thời lượng min (giây)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(options, from_=10, to=180, width=7, textvariable=self.min_duration_var).grid(
            row=0, column=1, sticky="w", padx=(6, 18)
        )
        ttk.Label(options, text="Max (giây)").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(options, from_=10, to=180, width=7, textvariable=self.max_duration_var).grid(
            row=0, column=3, sticky="w", padx=(6, 18)
        )
        ttk.Label(options, text="Số short tối đa").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(options, from_=1, to=50, width=6, textvariable=self.max_shorts_var).grid(
            row=0, column=5, sticky="w", padx=(6, 18)
        )
        ttk.Label(options, text="Video dọc").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            options,
            textvariable=self.vertical_mode_var,
            values=VERTICAL_MODES,
            state="readonly",
            width=28,
        ).grid(row=1, column=1, columnspan=3, sticky="w", padx=(6, 18), pady=(8, 0))
        ttk.Checkbutton(
            options, text="Burn phụ đề vào video", variable=self.burn_subtitles_var
        ).grid(row=1, column=4, columnspan=2, sticky="w", pady=(8, 0))

        instructions = ttk.LabelFrame(main, text="Yêu cầu thêm cho Gemini (không bắt buộc)", padding=6)
        instructions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        instructions.columnconfigure(0, weight=1)
        self.instructions_text = scrolledtext.ScrolledText(instructions, height=4, wrap="word")
        self.instructions_text.grid(row=0, column=0, sticky="ew")

        actions = ttk.Frame(main)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="Tạo Shorts", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions, text="Dừng", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Mở thư mục output", command=self._open_output).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        output_line = ttk.Frame(main)
        output_line.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        output_line.columnconfigure(1, weight=1)
        ttk.Label(output_line, text="Kết quả gần nhất").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(output_line, textvariable=self.last_output_var, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )

        logs = ttk.LabelFrame(main, text="Log realtime", padding=6)
        logs.grid(row=7, column=0, columnspan=3, sticky="nsew")
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(logs, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

    @staticmethod
    def _file_row(parent: ttk.LabelFrame, row: int, label: str, variable: tk.StringVar, command: Callable[[], None]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="Chọn...", command=command).grid(row=row, column=2, padx=(8, 0), pady=3)

    def _choose_videos(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Chọn một hoặc nhiều video",
            filetypes=(("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"), ("Tất cả", "*.*")),
        )
        for raw in paths:
            normalized = str(Path(raw))
            if normalized not in self.video_paths:
                self.video_paths.append(normalized)
                self.video_list.insert("end", normalized)

    def _remove_videos(self) -> None:
        for index in reversed(self.video_list.curselection()):
            self.video_list.delete(index)
            del self.video_paths[index]

    def _clear_videos(self) -> None:
        self.video_list.delete(0, "end")
        self.video_paths.clear()

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Chọn thư mục output")
        if path:
            self.output_dir_var.set(path)

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.events.put(("log", f"[{stamp}] {message}"))

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _load_settings(self) -> None:
        if not SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            self.model_var.set(str(data.get("model", self.model_var.get())))
            self.srt_model_var.set(str(data.get("srt_model", DEFAULT_SRT_MODEL)))
            self.auto_srt_var.set(bool(data.get("auto_srt", True)))
            self.video_parallel_var.set(str(data.get("video_parallel", DEFAULT_VIDEO_PARALLEL)))
            self.srt_parallel_var.set(str(data.get("srt_parallel", DEFAULT_SRT_PARALLEL)))
            for raw in data.get("video_paths", []):
                if Path(str(raw)).is_file() and str(raw) not in self.video_paths:
                    self.video_paths.append(str(raw))
                    self.video_list.insert("end", str(raw))
            self.output_dir_var.set(str(data.get("output_dir", DEFAULT_OUTPUT_DIR)))
            self.min_duration_var.set(str(data.get("min_duration", 25)))
            self.max_duration_var.set(str(data.get("max_duration", 60)))
            self.max_shorts_var.set(str(data.get("max_shorts", 10)))
            self.api_concurrency_var.set(
                str(data.get("api_concurrency", DEFAULT_API_CONCURRENCY))
            )
            mode = str(data.get("vertical_mode", VERTICAL_BLUR))
            self.vertical_mode_var.set(mode if mode in VERTICAL_MODES else VERTICAL_BLUR)
            self.burn_subtitles_var.set(bool(data.get("burn_subtitles", True)))
            self.remember_key_var.set(bool(data.get("remember_api_key", False)))
            if self.remember_key_var.get():
                self.api_key_var.set(str(data.get("api_key", self.api_key_var.get())))
                self.srt_api_key_var.set(
                    str(data.get("srt_api_key", self.srt_api_key_var.get()))
                )
            self.instructions_text.insert("1.0", str(data.get("extra_instructions", "")))
        except Exception as exc:
            self._append_log(f"Không đọc được settings: {exc}")

    def _save_settings(self) -> None:
        remember = self.remember_key_var.get()
        data = {
            "model": self.model_var.get().strip(),
            "srt_model": self.srt_model_var.get().strip(),
            "auto_srt": self.auto_srt_var.get(),
            "video_parallel": self.video_parallel_var.get().strip(),
            "srt_parallel": self.srt_parallel_var.get().strip(),
            "video_paths": list(self.video_paths),
            "output_dir": self.output_dir_var.get().strip(),
            "min_duration": self.min_duration_var.get().strip(),
            "max_duration": self.max_duration_var.get().strip(),
            "max_shorts": self.max_shorts_var.get().strip(),
            "api_concurrency": self.api_concurrency_var.get().strip(),
            "vertical_mode": self.vertical_mode_var.get(),
            "burn_subtitles": self.burn_subtitles_var.get(),
            "extra_instructions": self.instructions_text.get("1.0", "end").strip(),
            "remember_api_key": remember,
            "api_key": self.api_key_var.get().strip() if remember else "",
            "srt_api_key": self.srt_api_key_var.get().strip() if remember else "",
        }
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _collect_config(self) -> RunConfig:
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        srt_api_key = self.srt_api_key_var.get().strip()
        srt_model = self.srt_model_var.get().strip()
        auto_srt = self.auto_srt_var.get()
        output_text = self.output_dir_var.get().strip()
        if not api_key:
            raise ValueError("Bạn chưa nhập Gemini API Key B (lên plan).")
        if not model:
            raise ValueError("Bạn chưa chọn/nhập model B (lên plan).")
        if not self.video_paths:
            raise ValueError("Chưa chọn video nào.")
        videos = tuple(Path(raw) for raw in self.video_paths)
        for video in videos:
            if not video.is_file():
                raise ValueError(f"File video không tồn tại: {video}")
        missing = [v.name for v in videos if not video_srt_path(v).is_file()]
        if missing and not auto_srt:
            raise ValueError("Thiếu file .srt cùng tên (hoặc bật tự lấy SRT) cho: " + ", ".join(missing))
        if missing and not (srt_api_key and srt_model):
            raise ValueError("Cần API Key A và model A để tự lấy SRT cho: " + ", ".join(missing))
        if not output_text:
            raise ValueError("Bạn chưa chọn thư mục output.")
        min_duration = parse_number(self.min_duration_var.get(), "Thời lượng min", 10, 180)
        max_duration = parse_number(self.max_duration_var.get(), "Thời lượng max", 10, 180)
        if min_duration > max_duration:
            raise ValueError("Thời lượng min không được lớn hơn max.")
        return RunConfig(
            api_key=api_key,
            model=model,
            srt_api_key=srt_api_key,
            srt_model=srt_model,
            auto_srt=auto_srt,
            video_paths=tuple(v.resolve() for v in videos),
            video_parallel=parse_number(
                self.video_parallel_var.get(), "Số video song song", 1, MAX_VIDEO_PARALLEL
            ),
            srt_parallel=parse_number(
                self.srt_parallel_var.get(), "Video lấy SRT cùng lúc", 1, MAX_VIDEO_PARALLEL
            ),
            output_dir=Path(output_text).resolve(),
            min_duration=min_duration,
            max_duration=max_duration,
            max_shorts=parse_number(self.max_shorts_var.get(), "Số short", 1, 50),
            api_concurrency=parse_number(
                self.api_concurrency_var.get(), "Request API song song", 1, MAX_API_CONCURRENCY
            ),
            vertical_mode=self.vertical_mode_var.get(),
            burn_subtitles=self.burn_subtitles_var.get(),
            extra_instructions=self.instructions_text.get("1.0", "end").strip(),
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._collect_config()
            ensure_dependencies()
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Không thể bắt đầu", str(exc))
            return
        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Đang chạy...")
        self.worker = threading.Thread(target=self._run, args=(config,), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("Đang dừng...")
            self._log("Đã nhận yêu cầu dừng; sẽ dừng ở điểm an toàn gần nhất.")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise InterruptedError("Đã dừng theo yêu cầu.")

    def _run(self, config: RunConfig) -> None:
        """Process every video; up to config.video_parallel videos run at the same time."""
        srt_slots = threading.Semaphore(config.srt_parallel)
        results: list[tuple[Path, str, str]] = []  # (video, run_dir, error)

        def job(video: Path) -> tuple[Path, str, str]:
            def log(message: str, name: str = video.name) -> None:
                self._log(f"[{name}] {message}")

            try:
                run_dir = self._process_video(config, video, log, srt_slots)
                return video, str(run_dir), ""
            except InterruptedError:
                raise
            except Exception as exc:
                log(traceback.format_exc())
                return video, "", str(exc)

        try:
            count = len(config.video_paths)
            workers = max(1, min(config.video_parallel, count))
            self._log(f"Bắt đầu {count} video, chạy song song {workers} video.")
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video") as pool:
                futures = [pool.submit(job, video) for video in config.video_paths]
                try:
                    for future in as_completed(futures):
                        result = future.result()
                        results.append(result)
                        status = "lỗi: " + result[2] if result[2] else "xong"
                        self._log(f"[{result[0].name}] {status} ({len(results)}/{count})")
                except BaseException:
                    self.cancel_event.set()
                    for future in futures:
                        future.cancel()
                    raise
            self.events.put(("complete", (config.output_dir, results)))
        except InterruptedError as exc:
            self.events.put(("cancelled", str(exc)))
        except Exception as exc:
            self._log(traceback.format_exc())
            self.events.put(("error", (str(exc), "")))

    def _process_video(
        self,
        config: RunConfig,
        video_path: Path,
        log: Callable[[str], None],
        srt_slots: threading.Semaphore,
    ) -> Path:
        self._check_cancelled()
        srt_path = video_srt_path(video_path)
        if not srt_path.is_file():
            with srt_slots:
                self._check_cancelled()
                self._create_srt(config, video_path, srt_path, log)
        log(f"Model plan: {config.model}")
        log("Đang đọc SRT và kiểm tra video...")
        srt_text, encoding, repaired = read_srt_text(srt_path)
        cues = parse_srt(srt_text)
        if not cues:
            raise RuntimeError("SRT không có cue hợp lệ.")
        video_duration = probe_duration(video_path)
        if cues[0].start >= video_duration:
            raise RuntimeError("Timestamp SRT nằm ngoài thời lượng video.")
        cues = [cue for cue in cues if cue.start < video_duration]
        log(f"Đọc {len(cues)} subtitle ({encoding}); video dài {format_timestamp(video_duration)}.")
        if repaired:
            log("Đã tự sửa lỗi encoding/mojibake trong nội dung SRT.")

        client = GeminiShortClient(config.api_key, config.model, log, self.cancel_event)
        chunks = chunk_cues(cues)
        candidate_limit = min(
            config.max_shorts, max(2, math.ceil(config.max_shorts / len(chunks)) + 2)
        )
        analysis_jobs: list[Callable[[], Any]] = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            self._check_cancelled()
            log(
                f"Xếp hàng phân tích phần {chunk_index}/{len(chunks)} "
                f"({format_timestamp(chunk[0].start)} - {format_timestamp(chunk[-1].end)})..."
            )
            analysis_jobs.append(
                lambda chunk=chunk, chunk_index=chunk_index: client.find_candidates(
                    chunk,
                    config.min_duration,
                    config.max_duration,
                    candidate_limit,
                    config.extra_instructions,
                    chunk_index,
                    len(chunks),
                )
            )
        analysis_payloads = run_parallel_api_jobs(
            analysis_jobs, config.api_concurrency, self.cancel_event
        )
        candidates: list[ShortPlan] = []
        for chunk_index, (chunk, payload) in enumerate(zip(chunks, analysis_payloads), start=1):
            normalized = normalize_candidates(
                payload, chunk, config.min_duration, config.max_duration, video_duration
            )
            candidates.extend(normalized)
            log(f"Phần {chunk_index}: nhận {len(normalized)} ứng viên hợp lệ.")
        plans = select_best_candidates(candidates, config.max_shorts)
        if not plans:
            raise RuntimeError(
                "Gemini không chọn được đoạn nào hợp lệ. Hãy giảm thời lượng min hoặc đổi yêu cầu thêm."
            )
        log(f"Đã chọn {len(plans)} short sau khi loại đoạn trùng.")

        run_dir = build_run_directory(config.output_dir / safe_filename(video_path.stem, "video"))
        log(f"Thư mục phiên chạy: {run_dir}")
        metadata_jobs: list[Callable[[], Any]] = []
        for index, plan in enumerate(plans, start=1):
            self._check_cancelled()
            segment_cues = cues_for_range(cues, plan.start, plan.end)
            metadata_jobs.append(
                lambda plan=plan, segment_cues=segment_cues, index=index: client.create_metadata(
                    plan, segment_cues, index, video_path.name
                )
            )
        log(f"Tạo metadata cho {len(metadata_jobs)} short...")
        metadata_results = run_parallel_api_jobs(
            metadata_jobs, config.api_concurrency, self.cancel_event
        )

        for index, (plan, metadata) in enumerate(zip(plans, metadata_results), start=1):
            self._check_cancelled()
            plan.title = metadata["title"]
            plan.description = metadata["description"]
            plan.thumbnail_prompt = metadata["thumbnail_prompt"]

            slug = safe_filename(plan.title, f"short_{index:02d}")
            prefix = f"short_{index:02d}_{slug}"
            clip_srt = run_dir / f"{prefix}.srt"
            metadata_path = run_dir / f"{prefix}_metadata.txt"
            clip_video = run_dir / f"{prefix}.mp4"
            write_clipped_srt(cues, plan.start, plan.end, clip_srt)
            write_metadata(metadata_path, plan, index)
            plan.subtitle_path = str(clip_srt)
            plan.metadata_path = str(metadata_path)
            log(f"Short {index}: đang cắt và chuyển sang video dọc 1080x1920...")
            run_ffmpeg(
                video_path,
                clip_video,
                plan.start,
                plan.duration,
                config.vertical_mode,
                clip_srt if config.burn_subtitles else None,
                log,
                self.cancel_event,
            )
            plan.video_path = str(clip_video)
            log(f"Short {index} hoàn tất: {clip_video.name}")
            self._write_plan_json(run_dir, config, video_path, srt_path, plans, video_duration, repaired)

        self._write_plan_json(run_dir, config, video_path, srt_path, plans, video_duration, repaired)
        return run_dir

    def _create_srt(
        self, config: RunConfig, video_path: Path, srt_path: Path, log: Callable[[str], None]
    ) -> None:
        if not config.auto_srt:
            raise RuntimeError(f"Thiếu file SRT: {srt_path.name}")
        duration = probe_duration(video_path)
        log(f"Chưa có SRT, Gemini A ({config.srt_model}) đang tách SRT từ video...")
        client = GeminiJsonClient(config.srt_api_key, config.srt_model, log, self.cancel_event)
        cues = transcribe_media(
            client, video_path, duration, config.output_dir / "_srt_work", log, self.cancel_event
        )
        write_generated_srt(cues, srt_path)
        log(f"Đã tạo SRT: {srt_path} ({len(cues)} cue).")

    @staticmethod
    def _write_plan_json(
        run_dir: Path,
        config: RunConfig,
        video_path: Path,
        srt_path: Path,
        plans: Sequence[ShortPlan],
        video_duration: float,
        srt_encoding_repaired: bool,
    ) -> None:
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_video": str(video_path),
            "source_srt": str(srt_path),
            "source_duration_seconds": round(video_duration, 3),
            "model": config.model,
            "srt_model": config.srt_model,
            "api_concurrency": config.api_concurrency,
            "vertical_mode": config.vertical_mode,
            "burn_subtitles": config.burn_subtitles,
            "srt_encoding_repaired": srt_encoding_repaired,
            "shorts": [asdict(plan) | {"duration": round(plan.duration, 3)} for plan in plans],
        }
        (run_dir / "shorts_plan.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _flush_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "complete":
                    output_dir, results = payload
                    self.last_output_var.set(str(output_dir))
                    failed = [(video, error) for video, _, error in results if error]
                    self.status_var.set(
                        "Hoàn tất" if not failed else f"Xong, {len(failed)} video lỗi"
                    )
                    self._set_idle()
                    summary = f"Đã xử lý {len(results)} video, kết quả tại:\n{output_dir}"
                    if failed:
                        summary += "\n\nVideo lỗi:\n" + "\n".join(
                            f"- {video.name}: {error}" for video, error in failed
                        )
                        messagebox.showwarning("Hoàn tất (có lỗi)", summary)
                    else:
                        messagebox.showinfo("Hoàn tất", summary)
                elif kind == "cancelled":
                    self.status_var.set("Đã dừng")
                    self._set_idle()
                    self._append_log(str(payload))
                elif kind == "error":
                    message, partial_dir = payload
                    if partial_dir:
                        self.last_output_var.set(partial_dir)
                    self.status_var.set("Có lỗi")
                    self._set_idle()
                    messagebox.showerror("Tạo short thất bại", message)
        except queue.Empty:
            pass
        self.root.after(120, self._flush_events)

    def _set_idle(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _open_output(self) -> None:
        raw_path = self.last_output_var.get().strip() or self.output_dir_var.get().strip()
        path = Path(raw_path)
        if not path.exists():
            messagebox.showinfo("Chưa có output", "Thư mục output chưa tồn tại.")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Không mở được thư mục", str(exc))

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Tool đang chạy", "Dừng tác vụ và đóng tool?"):
                return
            self.cancel_event.set()
        try:
            self._save_settings()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = ShortCutterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
