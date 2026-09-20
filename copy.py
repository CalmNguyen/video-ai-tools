"""
Required packages:
    pip install requests google-genai cryptography Pillow

This tool is a single-file desktop app for Windows that:
1. Turns an idea into scene JSON with Gemini text.
2. Generates one image per scene with Imagen 4.
3. Builds an SRT file.
4. Calls the real TTS For Free SRT-to-speech API flow:
   POST /api/TTS/CreateSrt
   GET  /api/TTS/Multi/Status/{jobid}
5. Downloads the audio.
6. Creates the final video with FFmpeg.

Notes:
- The current TTS For Free backend expects encrypted voice tokens for CreateSrt.
  This tool accepts a plain voice string in the UI and encrypts it automatically
  with the same V1 algorithm used by the backend when needed.
- Imagen 4 models are deprecated by Google. They are kept here because the
  requested prompt explicitly asked for them.
"""

from __future__ import annotations

import base64
import ast
import json
import math
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


def _load_stdlib_copy_before_local_modules() -> None:
    """Prevent a neighboring copy.py file from shadowing Python's stdlib copy."""
    existing = sys.modules.get("copy")
    existing_file = getattr(existing, "__file__", "") if existing else ""
    local_copy_path = Path(__file__).resolve().with_name("copy.py")
    if existing and (
        not existing_file or Path(existing_file).resolve() != local_copy_path
    ):
        return

    if existing:
        sys.modules.pop("copy", None)

    original_sys_path = list(sys.path)
    script_directory = Path(__file__).resolve().parent
    try:
        filtered_path: list[str] = []
        for entry in original_sys_path:
            candidate = Path(entry or os.getcwd()).resolve()
            if candidate != script_directory:
                filtered_path.append(entry)
        sys.path[:] = filtered_path
        __import__("copy")
    finally:
        sys.path[:] = original_sys_path


_load_stdlib_copy_before_local_modules()

try:
    import requests
except ImportError:  # pragma: no cover - handled at runtime
    requests = None

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - handled at runtime
    AESGCM = None

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - handled at runtime
    genai = None
    types = None


LogFn = Callable[[str], None]


def parse_positive_int(value: Any, field_name: str = "value") -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer.") from exc

    if parsed < 1:
        raise ValueError(f"{field_name} must be a positive integer.")
    return parsed


def positive_int_or_default(value: Any, default: int) -> int:
    try:
        return parse_positive_int(value)
    except ValueError:
        return default


def positive_int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = SCRIPT_DIR / "outputs"
TEMP_ROOT = SCRIPT_DIR / "temp"
SETTINGS_PATH = SCRIPT_DIR / "gemini_ttsforfree_video_tool.settings.json"
HISTORY_PATH = SCRIPT_DIR / "gemini_ttsforfree_video_tool.history.json"
MAX_HISTORY_ENTRIES = 100
HISTORY_LOCK = threading.Lock()
SUPPORTED_REFERENCE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
REFERENCE_IMAGE_FILETYPES = (
    ("Image files", "*.png *.jpg *.jpeg *.webp"),
    ("All files", "*.*"),
)

GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash").strip()
DEFAULT_TTS_ENDPOINT = os.getenv("TTSFORFREE_ENDPOINT", "https://api.ttsforfree.com").strip()
TTSFORFREE_CREATE_SRT_PATH = "/api/TTS/CreateSrt"
TTSFORFREE_STATUS_PATH_TEMPLATE = "/api/TTS/Multi/Status/{job_id}"
TTSFORFREE_DEFAULT_SPEED = float(os.getenv("TTSFORFREE_SPEED", "0.9"))
TTSFORFREE_DEFAULT_PITCH = float(os.getenv("TTSFORFREE_PITCH", "0"))
TTSFORFREE_DEFAULT_VOLUME = float(os.getenv("TTSFORFREE_VOLUME", "1"))
TTSFORFREE_POLL_INTERVAL_SECONDS = 5
TTSFORFREE_POLL_TIMEOUT_SECONDS = 20 * 60
REQUEST_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_SIZE = 1024 * 512
GEMINI_RETRY_DELAYS = (10, 30, 60)
IMAGE_GENERATION_MAX_WORKERS = positive_int_or_default(
    os.getenv("IMAGE_GENERATION_MAX_WORKERS", "10"),
    3,
)
VIDEO_FPS = 30
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VOICE_CRYPTO_KEY_B64 = "K1hXK1B4K0pMaHBDaHh6WXh1R2J4S3ZBbkJoQ2l4eWw="

IMAGE_MODEL_OPTIONS = {
    # --- Dòng Gemini (Tối ưu chi phí cực thấp, tốc độ nhanh) ---
    "Gemini 3.1 Flash-Lite Image | Fastest, lowest cost": {
        "id": "gemini-3.1-flash-lite-image",
        "family": "gemini_image",
    },
    "Gemini 3.1 Flash Image | Balanced, supports higher resolution": {
        "id": "gemini-3.1-flash-image",
        "family": "gemini_image",
    },
    "Gemini 3 Pro Image | Highest quality, complex detail": {
        "id": "gemini-3-pro-image",
        "family": "gemini_image",
    },
    
    # --- Dòng Imagen 4 (Vertex AI - Tối ưu chất lượng ảnh và text render) ---
    "Imagen 4 Fast Generate | Deprecated 2026-08-17": {
        "id": "imagen-4.0-fast-generate-001",
        "family": "imagen",
    },
    "Imagen 4 Generate | Deprecated 2026-08-17": {
        "id": "imagen-4.0-generate-001",
        "family": "imagen",
    },
    "Imagen 4 Ultra Generate | Deprecated 2026-08-17": {
        "id": "imagen-4.0-ultra-generate-001",
        "family": "imagen",
    },
}

DEFAULT_VOICE_OPTIONS = [
    "v1:ZX7_aRS0FzGeqAgWsfVbUP_St5VDMhy9r58iPvgQ-YKffu7Y83VDYXS0",
]

BATCH_MODE_SEQUENTIAL = "Sequential"
BATCH_MODE_PARALLEL = "Parallel"
BATCH_MODE_OPTIONS = (BATCH_MODE_SEQUENTIAL, BATCH_MODE_PARALLEL)
VIDEO_MODE_SHORT = "Short"
VIDEO_MODE_LONG = "Long"
VIDEO_MODE_OPTIONS = (VIDEO_MODE_SHORT, VIDEO_MODE_LONG)

TTSFORFREE_API_KEY = os.getenv("TTSFORFREE_API_KEY", "").strip()
TTSFORFREE_BEARER_TOKEN = os.getenv("TTSFORFREE_BEARER_TOKEN", "").strip()
TTSFORFREE_COOKIE = os.getenv("TTSFORFREE_COOKIE", "").strip()
TTSFORFREE_EXTRA_HEADERS_RAW = os.getenv("TTSFORFREE_EXTRA_HEADERS", "").strip()


class RetryableError(RuntimeError):
    """Signals that retry_with_backoff should try again."""


class ImageGenerationNoOutputError(RetryableError):
    """Raised when Gemini image generation finishes without an image part."""


class TtsJobFailedError(RuntimeError):
    """Raised when TTS For Free returns a failed job."""


class Scene:
    def __init__(
        self,
        sceneIndex: int,
        narrationText: str,
        imagePrompt: str,
        durationSeconds: int,
    ) -> None:
        self.sceneIndex = sceneIndex
        self.narrationText = narrationText
        self.imagePrompt = imagePrompt
        self.durationSeconds = durationSeconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "sceneIndex": self.sceneIndex,
            "narrationText": self.narrationText,
            "imagePrompt": self.imagePrompt,
            "durationSeconds": self.durationSeconds,
        }


class ReferenceImage:
    def __init__(self, path: str, mime_type: str, data: bytes) -> None:
        self.path = path
        self.mime_type = mime_type
        self.data = data


class TokenUsageTracker:
    """Thread-safe per-video aggregation of Gemini response usage metadata."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._input_tokens = 0
        self._output_tokens = 0
        self._reported_total_tokens = 0
        self._thoughts_tokens = 0
        self._cached_content_tokens = 0
        self._api_requests = 0
        self._requests_with_metadata = 0
        self._requests_missing_token_counts = 0
        self._operations: dict[str, dict[str, int]] = {}

    @staticmethod
    def _count(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _metadata_value(metadata: Any, *names: str) -> Any:
        if isinstance(metadata, dict):
            for name in names:
                if name in metadata:
                    return metadata[name]
            return None
        for name in names:
            value = getattr(metadata, name, None)
            if value is not None:
                return value
        return None

    def record_response(self, response: Any, operation: str) -> None:
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None and isinstance(response, dict):
            metadata = response.get("usage_metadata") or response.get("usageMetadata")

        input_tokens = self._count(
            self._metadata_value(metadata, "prompt_token_count", "promptTokenCount")
        )
        output_tokens = self._count(
            self._metadata_value(
                metadata,
                "candidates_token_count",
                "candidatesTokenCount",
                "response_token_count",
                "responseTokenCount",
            )
        )
        total_tokens = self._count(
            self._metadata_value(metadata, "total_token_count", "totalTokenCount")
        )
        thoughts_tokens = self._count(
            self._metadata_value(metadata, "thoughts_token_count", "thoughtsTokenCount")
        )
        cached_tokens = self._count(
            self._metadata_value(
                metadata,
                "cached_content_token_count",
                "cachedContentTokenCount",
            )
        )

        with self._lock:
            self._api_requests += 1
            operation_usage = self._operations.setdefault(
                operation,
                {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reported_total_tokens": 0,
                    "missing_token_counts": 0,
                },
            )
            operation_usage["requests"] += 1

            if metadata is not None:
                self._requests_with_metadata += 1
            if input_tokens is None or output_tokens is None:
                self._requests_missing_token_counts += 1
                operation_usage["missing_token_counts"] += 1

            self._input_tokens += input_tokens or 0
            self._output_tokens += output_tokens or 0
            self._reported_total_tokens += total_tokens or 0
            self._thoughts_tokens += thoughts_tokens or 0
            self._cached_content_tokens += cached_tokens or 0
            operation_usage["input_tokens"] += input_tokens or 0
            operation_usage["output_tokens"] += output_tokens or 0
            operation_usage["reported_total_tokens"] += total_tokens or 0

    def record_unreported_request(self, operation: str) -> None:
        """Record an API attempt that failed before returning usage metadata."""
        with self._lock:
            self._api_requests += 1
            self._requests_missing_token_counts += 1
            operation_usage = self._operations.setdefault(
                operation,
                {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reported_total_tokens": 0,
                    "missing_token_counts": 0,
                },
            )
            operation_usage["requests"] += 1
            operation_usage["missing_token_counts"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            operations = {
                name: dict(values) for name, values in self._operations.items()
            }
            return {
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "reported_total_tokens": self._reported_total_tokens,
                "thoughts_tokens": self._thoughts_tokens,
                "cached_content_tokens": self._cached_content_tokens,
                "api_requests": self._api_requests,
                "requests_with_metadata": self._requests_with_metadata,
                "requests_missing_token_counts": self._requests_missing_token_counts,
                "complete": self._requests_missing_token_counts == 0,
                "operations": operations,
            }


def format_token_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "Tokens | Input: 0 | Output: 0"
    input_tokens = positive_int_or_zero(usage.get("input_tokens"))
    output_tokens = positive_int_or_zero(usage.get("output_tokens"))
    api_requests = positive_int_or_zero(usage.get("api_requests"))
    missing = positive_int_or_zero(usage.get("requests_missing_token_counts"))
    suffix = f" | Requests: {api_requests}"
    if missing:
        suffix += f" | Missing token metadata: {missing}"
    return (
        f"Tokens | Input: {input_tokens:,} | Output: {output_tokens:,}{suffix}"
    )


class VideoJobRequest:
    def __init__(
        self,
        job_id: str,
        position: int,
        video_mode: str,
        idea: str,
        voice: str,
        video_prompt: str,
        use_shared_reference_images: bool,
        reference_image_paths: tuple[str, ...],
    ) -> None:
        self.job_id = job_id
        self.position = position
        self.video_mode = video_mode
        self.idea = idea
        self.voice = voice
        self.video_prompt = video_prompt
        self.use_shared_reference_images = use_shared_reference_images
        self.reference_image_paths = reference_image_paths


class VideoJobCard:
    def __init__(
        self,
        job_id: str,
        frame: ttk.LabelFrame,
        video_mode_var: tk.StringVar,
        voice_var: tk.StringVar,
        video_prompt_text: scrolledtext.ScrolledText,
        idea_text: scrolledtext.ScrolledText,
        use_shared_references_var: tk.BooleanVar,
        reference_image_paths: list[str],
        reference_images_var: tk.StringVar,
        status_var: tk.StringVar,
        token_usage_var: tk.StringVar,
    ) -> None:
        self.job_id = job_id
        self.frame = frame
        self.video_mode_var = video_mode_var
        self.voice_var = voice_var
        self.video_prompt_text = video_prompt_text
        self.idea_text = idea_text
        self.use_shared_references_var = use_shared_references_var
        self.reference_image_paths = reference_image_paths
        self.reference_images_var = reference_images_var
        self.status_var = status_var
        self.token_usage_var = token_usage_var


def ensure_runtime_dependencies() -> None:
    missing: list[str] = []
    if requests is None:
        missing.append("requests")
    if AESGCM is None:
        missing.append("cryptography")
    if genai is None or types is None:
        missing.append("google-genai")

    if missing:
        raise RuntimeError(
            "Missing packages: "
            + ", ".join(missing)
            + ". Install with: pip install requests google-genai cryptography Pillow"
        )


def ensure_ffmpeg_installed() -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError(
            "FFmpeg or ffprobe was not found in PATH. Please install FFmpeg first."
        )


def load_extra_headers() -> dict[str, str]:
    if not TTSFORFREE_EXTRA_HEADERS_RAW:
        return {}
    try:
        value = json.loads(TTSFORFREE_EXTRA_HEADERS_RAW)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TTSFORFREE_EXTRA_HEADERS must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("TTSFORFREE_EXTRA_HEADERS must be a JSON object.")
    return {str(k): str(v) for k, v in value.items()}


def format_srt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, remainder = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{secs:02},{remainder:03}"


def estimate_duration(text: str) -> int:
    clean_text = re.sub(r"\s+", " ", (text or "").strip())
    if not clean_text:
        return 3

    words = re.findall(r"\S+", clean_text)
    punctuation_bonus = len(re.findall(r"[,.!?;:]", clean_text)) * 0.12
    base_seconds = len(words) / 2.6 + punctuation_bonus
    return max(3, int(math.ceil(base_seconds)))


def write_srt(scenes: Sequence[Scene], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    current = 0.0
    for index, scene in enumerate(scenes, start=1):
        start = current
        duration = max(1, int(scene.durationSeconds))
        current += duration
        lines.append(str(index))
        lines.append(f"{format_srt_timestamp(start)} --> {format_srt_timestamp(current)}")
        lines.append(scene.narrationText.strip())
        lines.append("")

    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def build_plain_script_from_scenes(scenes: Sequence[Scene]) -> str:
    return "\n".join(
        f"{scene.sceneIndex}. {scene.narrationText.strip()}"
        for scene in scenes
        if scene.narrationText.strip()
    )


def build_plain_script_from_scene_payload(payload: Any) -> str:
    try:
        scenes = normalize_scenes(payload)
    except Exception:
        return ""
    return build_plain_script_from_scenes(scenes)


def read_text_if_exists(path: str | Path) -> str:
    try:
        file_path = Path(path)
        if file_path.exists():
            return file_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    return ""


def load_script_text_from_paths(scenes_json_path: str | Path, srt_path: str | Path) -> str:
    try:
        scenes_path = Path(scenes_json_path)
        if scenes_path.exists():
            payload = json.loads(scenes_path.read_text(encoding="utf-8"))
            script_text = build_plain_script_from_scene_payload(payload)
            if script_text:
                return script_text
    except Exception:
        pass

    srt_text = read_text_if_exists(srt_path)
    if not srt_text:
        return ""

    lines: list[str] = []
    for line in srt_text.splitlines():
        clean_line = line.strip()
        if not clean_line or clean_line.isdigit() or "-->" in clean_line:
            continue
        lines.append(clean_line)
    return "\n".join(lines)


def open_path_with_default_app(path: str | Path) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")

    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return

    opener = "open" if shutil.which("open") else "xdg-open"
    subprocess.Popen([opener, str(target)])


def load_history_file_entries() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []

    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    return [entry for entry in data if isinstance(entry, dict)]


def save_history_file_entries(entries: Sequence[dict[str, Any]]) -> None:
    HISTORY_PATH.write_text(
        json.dumps(list(entries)[:MAX_HISTORY_ENTRIES], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def history_entry_key(entry: dict[str, Any]) -> str:
    run_id = str(entry.get("run_id", "")).strip()
    if run_id:
        return run_id
    return str(entry.get("final_video_path", "")).strip()


def append_generation_history(entry: dict[str, Any]) -> None:
    # Several videos may finish at the same time in parallel batch mode.
    # Keep the read/modify/write cycle atomic so one completion cannot erase another.
    with HISTORY_LOCK:
        entries = load_history_file_entries()
        new_key = history_entry_key(entry)
        deduped = [
            existing
            for existing in entries
            if history_entry_key(existing) and history_entry_key(existing) != new_key
        ]
        save_history_file_entries([entry, *deduped])


def discover_history_entries() -> list[dict[str, Any]]:
    if not OUTPUT_ROOT.exists():
        return []

    entries: list[dict[str, Any]] = []
    for final_video_path in OUTPUT_ROOT.glob("*/final_video.mp4"):
        try:
            stat = final_video_path.stat()
        except OSError:
            continue

        run_id = final_video_path.parent.name
        temp_dir = TEMP_ROOT / run_id
        scenes_json_path = temp_dir / "scenes.json"
        srt_path = temp_dir / "script.srt"
        token_usage_path = final_video_path.parent / "token_usage.json"
        token_usage: dict[str, Any] | None = None
        if token_usage_path.exists():
            try:
                loaded_usage = json.loads(token_usage_path.read_text(encoding="utf-8"))
                if isinstance(loaded_usage, dict):
                    token_usage = loaded_usage
            except Exception:
                token_usage = None
        entries.append(
            {
                "run_id": run_id,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "final_video_path": str(final_video_path),
                "output_dir": str(final_video_path.parent),
                "temp_dir": str(temp_dir),
                "scenes_json_path": str(scenes_json_path),
                "srt_path": str(srt_path),
                "video_mode": (
                    str(token_usage.get("video_mode", VIDEO_MODE_SHORT))
                    if token_usage
                    else VIDEO_MODE_SHORT
                ),
                "token_usage": token_usage,
                "token_usage_path": str(token_usage_path) if token_usage else "",
                "script_text": load_script_text_from_paths(scenes_json_path, srt_path),
                "srt_text": read_text_if_exists(srt_path),
            }
        )

    return entries


def load_generation_history() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for entry in discover_history_entries():
        key = history_entry_key(entry)
        if key:
            merged[key] = entry

    for entry in load_history_file_entries():
        key = history_entry_key(entry)
        if key:
            existing = merged.get(key, {})
            merged[key] = {**existing, **entry}

    entries = list(merged.values())
    entries.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return entries[:MAX_HISTORY_ENTRIES]


def format_history_entry_text(entry: dict[str, Any]) -> str:
    final_video_path = str(entry.get("final_video_path", "")).strip()
    output_dir = str(entry.get("output_dir", "")).strip()
    temp_dir = str(entry.get("temp_dir", "")).strip()
    scenes_json_path = str(entry.get("scenes_json_path", "")).strip()
    srt_path = str(entry.get("srt_path", "")).strip()
    script_text = str(entry.get("script_text", "")).strip()
    srt_text = str(entry.get("srt_text", "")).strip()
    reference_image_paths = entry.get("reference_image_paths", [])
    if isinstance(reference_image_paths, list):
        reference_images_text = "\n".join(str(path) for path in reference_image_paths)
    else:
        reference_images_text = ""
    token_usage = entry.get("token_usage")
    if not isinstance(token_usage, dict):
        if "input_tokens" in entry or "output_tokens" in entry:
            token_usage = {
                "input_tokens": entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
                "api_requests": entry.get("token_api_requests", 0),
                "requests_missing_token_counts": entry.get(
                    "token_requests_missing_counts", 0
                ),
            }
        else:
            token_usage = None

    if not script_text:
        script_text = load_script_text_from_paths(scenes_json_path, srt_path)
    if not srt_text:
        srt_text = read_text_if_exists(srt_path)

    parts = [
        f"Run ID: {entry.get('run_id', '')}",
        f"Created At: {entry.get('created_at', '')}",
        f"Final Video: {final_video_path}",
        f"Output Folder: {output_dir}",
        f"Temp Folder: {temp_dir}",
        f"Scenes JSON: {scenes_json_path}",
        f"SRT File: {srt_path}",
        f"Voice: {entry.get('voice', '')}",
        f"Video Mode: {entry.get('video_mode', VIDEO_MODE_SHORT)}",
        f"Image Model: {entry.get('image_model_label', '')}",
        f"Shared Prompt: {entry.get('shared_prompt', '')}",
        f"Video Prompt: {entry.get('video_prompt', '')}",
        f"Reference Source: {entry.get('reference_source', '')}",
        format_token_usage(token_usage) if token_usage else "Tokens: not recorded",
        f"Token Usage File: {entry.get('token_usage_path', '')}",
        "Reference Images:",
        reference_images_text or "(No reference images.)",
        "",
        "Plain Script:",
        script_text or "(No script text found.)",
        "",
        "SRT:",
        srt_text or "(No SRT text found.)",
    ]
    return "\n".join(parts)


def run_ffmpeg(
    args: Sequence[str],
    logger: LogFn | None = None,
    cwd: str | Path | None = None,
    timeout: int | None = None,
) -> None:
    command = ["ffmpeg", *args]
    if logger:
        logger("Running FFmpeg: " + " ".join(command))

    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or "Unknown FFmpeg error."
        raise RuntimeError(detail)


def download_file(
    url: str,
    path: str | Path,
    session: Any | None = None,
    headers: dict[str, str] | None = None,
    logger: LogFn | None = None,
) -> Path:
    if requests is None:
        raise RuntimeError("requests is not installed.")

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()

    if logger:
        logger(f"Downloading: {url}")

    with http.get(
        url,
        headers=headers or {},
        timeout=REQUEST_TIMEOUT_SECONDS,
        stream=True,
    ) as response:
        response.raise_for_status()
        with output_path.open("wb") as file_obj:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    file_obj.write(chunk)

    return output_path


def retry_with_backoff(
    fn: Callable[[], Any],
    delays: Sequence[int] = GEMINI_RETRY_DELAYS,
    logger: LogFn | None = None,
    should_retry: Callable[[Exception], bool] | None = None,
) -> Any:
    attempt = 0
    total_attempts = len(delays) + 1

    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            can_retry = attempt <= len(delays)
            retry_allowed = should_retry(exc) if should_retry else True
            if not can_retry or not retry_allowed:
                raise

            delay = delays[attempt - 1]
            if logger:
                logger(
                    f"Attempt {attempt}/{total_attempts} failed: {exc}. "
                    f"Retrying in {delay}s..."
                )
            time.sleep(delay)


def extract_json_from_text(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned empty content.")

    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()

    if text.startswith("{") or text.startswith("["):
        return text

    match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if match:
        return match.group(1)

    raise RuntimeError("Could not find valid JSON in Gemini response.")


def repair_common_json_issues(json_text: str) -> str:
    repaired = (json_text or "").strip().lstrip("\ufeff")
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"')
    repaired = repaired.replace("\u2018", "'").replace("\u2019", "'")

    # Gemini occasionally returns JavaScript-ish JSON. These repairs cover the
    # common scene schema without trying to become a full JS parser.
    for key in ("scenes", "sceneIndex", "narrationText", "imagePrompt", "durationSeconds"):
        repaired = re.sub(
            rf'(?<=[{{,\s]){key}\s*:',
            f'"{key}":',
            repaired,
        )

    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"^\s*//.*?$", "", repaired, flags=re.MULTILINE)
    repaired = re.sub(r"^\s*/\*.*?\*/\s*$", "", repaired, flags=re.MULTILINE | re.DOTALL)
    return repaired.strip()


def load_json_with_common_repairs(raw_text: str) -> Any:
    json_text = extract_json_from_text(raw_text)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as original_exc:
        repaired_text = repair_common_json_issues(json_text)
        if repaired_text != json_text:
            try:
                return json.loads(repaired_text)
            except json.JSONDecodeError:
                pass

        # Last local fallback for Python-like output using single quotes.
        try:
            parsed = ast.literal_eval(repaired_text)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass

        raise original_exc


def is_retryable_gemini_error(exc: Exception) -> bool:
    message = str(exc).lower()
    retry_signals = (
        "429",
        "rate limit",
        "too many requests",
        "quota",
        "503",
        "service unavailable",
        "504",
        "timeout",
        "internal server error",
    )
    return any(signal in message for signal in retry_signals) or isinstance(exc, RetryableError)


def parse_srt_timestamp(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise RuntimeError(f"Invalid SRT timestamp: {value}")

    hours, minutes, seconds, millis = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def parse_srt_cues(srt_text: str) -> list[dict[str, Any]]:
    normalized = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    blocks = re.split(r"\n\s*\n", normalized)
    cues: list[dict[str, Any]] = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue

        if re.fullmatch(r"\d+", lines[0].strip()):
            timing_line = lines[1].strip()
            text_lines = lines[2:]
        else:
            timing_line = lines[0].strip()
            text_lines = lines[1:]

        timing_match = re.fullmatch(
            r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})",
            timing_line,
        )
        if not timing_match:
            continue

        text_value = "\n".join(line.strip() for line in text_lines).strip()
        if not text_value:
            continue

        start_ms = parse_srt_timestamp(timing_match.group("start"))
        end_ms = parse_srt_timestamp(timing_match.group("end"))
        cues.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text_value,
            }
        )

    return cues


def load_srt_cues(path: str | Path) -> list[dict[str, Any]]:
    return parse_srt_cues(Path(path).read_text(encoding="utf-8"))


def probe_media_duration_seconds(path: str | Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "").strip() or "ffprobe failed.")

    try:
        return float((completed.stdout or "").strip())
    except ValueError as exc:
        raise RuntimeError("Could not parse ffprobe duration.") from exc


def ffmpeg_escape_subtitle_path(path: str | Path) -> str:
    value = str(Path(path).resolve()).replace("\\", "/")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value


def make_safe_ffmpeg_manifest_path(path: str | Path) -> str:
    return Path(path).resolve().as_posix().replace("'", r"'\''")


def normalize_scenes(payload: Any) -> list[Scene]:
    if isinstance(payload, dict) and isinstance(payload.get("scenes"), list):
        items = payload["scenes"]
    elif isinstance(payload, list):
        items = payload
    else:
        raise RuntimeError("Gemini scene JSON must be an array or an object with a 'scenes' array.")

    scenes: list[Scene] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        narration = str(item.get("narrationText", "")).strip()
        image_prompt = str(item.get("imagePrompt", "")).strip()
        duration_value = item.get("durationSeconds")

        if not narration or not image_prompt:
            continue

        try:
            duration = int(math.ceil(float(duration_value)))
        except (TypeError, ValueError):
            duration = estimate_duration(narration)

        scenes.append(
            Scene(
                sceneIndex=index,
                narrationText=narration,
                imagePrompt=image_prompt,
                durationSeconds=max(1, duration),
            )
        )

    if not scenes:
        raise RuntimeError("Gemini did not return any valid scenes.")

    return scenes


def encrypt_voice_token_if_needed(voice: str) -> str:
    token = (voice or "").strip()
    if not token:
        raise RuntimeError("Voice is required.")

    if token.startswith("v1:"):
        return token

    if AESGCM is None:
        raise RuntimeError("cryptography is required to encrypt the voice token.")

    key = base64.b64decode(VOICE_CRYPTO_KEY_B64)
    nonce = os.urandom(12)
    aes = AESGCM(key)
    ciphertext_with_tag = aes.encrypt(nonce, token.encode("utf-8"), None)
    cipher = ciphertext_with_tag[:-16]
    tag = ciphertext_with_tag[-16:]
    combined = nonce + tag + cipher
    encoded = base64.urlsafe_b64encode(combined).decode("ascii").rstrip("=")
    return "v1:" + encoded


def guess_extension_from_url(url: str, default_ext: str = ".mp3") -> str:
    match = re.search(r"\.([a-zA-Z0-9]{2,5})(?:$|\?)", url or "")
    if not match:
        return default_ext
    return "." + match.group(1).lower()


def build_run_paths() -> dict[str, Path]:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output_dir = OUTPUT_ROOT / run_id
    temp_dir = TEMP_ROOT / run_id
    images_dir = temp_dir / "images"

    for directory in (OUTPUT_ROOT, TEMP_ROOT, output_dir, temp_dir, images_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return {
        "run_id": Path(run_id),
        "output_dir": output_dir,
        "temp_dir": temp_dir,
        "images_dir": images_dir,
    }


def build_image_prompt(scene_prompt: str) -> str:
    style_suffix = (
        "Create exactly one finished image for this scene. "
        "Cinematic, YouTube educational, clean professional visual style, "
        "landscape 16:9 composition, highly detailed, natural lighting, "
        "no text overlay, no watermark."
    )
    return f"{scene_prompt.strip()}. {style_suffix}"


def normalize_reference_image_paths(paths: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for raw_path in paths:
        value = str(raw_path or "").strip().strip('"')
        if not value:
            continue

        path = Path(value).expanduser()
        if not path.exists() or not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_REFERENCE_IMAGE_EXTENSIONS:
            continue

        resolved = path.resolve()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue

        seen.add(key)
        normalized.append(str(resolved))

    return normalized


def summarize_reference_image_paths(paths: Sequence[str]) -> str:
    if not paths:
        return "No reference images selected"

    names = [Path(path).name for path in paths[:2]]
    suffix = "" if len(paths) <= 2 else f", +{len(paths) - 2} more"
    return f"{len(paths)} reference image(s): {', '.join(names)}{suffix}"


def guess_reference_image_mime_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"

    guessed_mime_type, _ = mimetypes.guess_type(str(path))
    if guessed_mime_type and guessed_mime_type.startswith("image/"):
        return guessed_mime_type
    return "image/png"


def load_reference_images(paths: Sequence[str]) -> list[ReferenceImage]:
    reference_images: list[ReferenceImage] = []
    for path in normalize_reference_image_paths(paths):
        file_path = Path(path)
        try:
            reference_images.append(
                ReferenceImage(
                    path=str(file_path),
                    mime_type=guess_reference_image_mime_type(file_path),
                    data=file_path.read_bytes(),
                )
            )
        except OSError:
            continue
    return reference_images


def build_gemini_image_contents(
    full_prompt: str,
    reference_images: Sequence[ReferenceImage],
) -> Any:
    if not reference_images:
        return full_prompt

    prompt_with_reference_note = (
        f"{full_prompt}\n\n"
        "Use the attached reference image(s) as visual guidance for product UI, "
        "layout language, colors, components, and overall tool identity. "
        "Do not copy any private data or exact sensitive text from the references; "
        "create a new scene that feels consistent with them."
    )
    parts = [types.Part.from_text(text=prompt_with_reference_note)]

    for index, reference_image in enumerate(reference_images, start=1):
        parts.append(
            types.Part.from_text(
                text=f"Reference image {index}: {Path(reference_image.path).name}"
            )
        )
        parts.append(
            types.Part.from_bytes(
                data=reference_image.data,
                mime_type=reference_image.mime_type,
            )
        )

    return parts


def is_gemini_image_model(model_name: str) -> bool:
    return model_name.startswith("gemini-") and model_name.endswith("-image")


def ci_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return default


def extract_model_text(response: Any) -> str:
    text_value = getattr(response, "text", None)
    if text_value:
        return text_value

    candidates = getattr(response, "candidates", None) or []
    texts: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_text = getattr(part, "text", None)
            if part_text:
                texts.append(part_text)

    return "\n".join(texts).strip()


def save_generated_image(image_obj: Any, path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if hasattr(image_obj, "save"):
        image_obj.save(output_path)
        return output_path

    data = getattr(image_obj, "data", None)
    if isinstance(data, (bytes, bytearray)):
        output_path.write_bytes(bytes(data))
        return output_path

    raise RuntimeError("Could not save generated image from SDK response.")


def extract_first_image_from_generate_content_response(response: Any) -> Any | None:
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            if hasattr(part, "as_image"):
                image_obj = part.as_image()
                if image_obj is not None:
                    return image_obj

            inline_data = getattr(part, "inline_data", None)
            if inline_data and hasattr(inline_data, "as_image"):
                image_obj = inline_data.as_image()
                if image_obj is not None:
                    return image_obj
    return None


class GeminiVideoClient:
    def __init__(
        self,
        api_key: str,
        logger: LogFn,
        token_usage: TokenUsageTracker | None = None,
    ):
        ensure_runtime_dependencies()
        self.client = genai.Client(api_key=api_key.strip())
        self.logger = logger
        self.token_usage = token_usage or TokenUsageTracker()

    def generate_scene_script(
        self,
        idea: str,
        shared_prompt: str = "",
        video_prompt: str = "",
        video_mode: str = VIDEO_MODE_SHORT,
    ) -> list[Scene]:
        if video_mode not in VIDEO_MODE_OPTIONS:
            raise ValueError(f"Unsupported video mode: {video_mode}")

        extra_instructions: list[str] = []
        if shared_prompt.strip():
            extra_instructions.append(
                "Shared instructions for every video in this batch:\n"
                + shared_prompt.strip()
            )
        if video_prompt.strip():
            extra_instructions.append(
                "Instructions for this video only:\n" + video_prompt.strip()
            )
        instructions_text = "\n\n".join(extra_instructions) or "(No additional instructions.)"

        if video_mode == VIDEO_MODE_LONG:
            planner_role = "You are a senior long-form video script and scene planner."
            mode_rules = (
                "- Treat the user's input as a completed long-form narration script.\n"
                "- Preserve the full meaning, order, facts, examples, and important details.\n"
                "- Do NOT summarize, aggressively shorten, or omit sections of the script.\n"
                "- Keep the original narration wording whenever possible; never rewrite it into "
                "a shorter version.\n"
                "- The combined narrationText values must cover the entire source script from "
                "beginning to end. Light punctuation cleanup is allowed.\n"
                "- Create as many scenes as needed for the complete script; long videos will "
                "usually need 20-120 scenes, but there is no fixed scene limit.\n"
                "- Each scene should cover one coherent visual beat.\n"
                "- narrationText may contain 1-3 natural sentences, usually 15-40 words and "
                "never more than 60 words.\n"
                "- Split long paragraphs across consecutive scenes without losing their content.\n"
                "- durationSeconds should usually be 5-15 seconds and match the narration.\n"
                "- Maintain visual continuity for recurring people, places, objects, and style.\n"
            )
            input_label = "Completed long-form narration script"
            max_output_tokens = 65536
        else:
            planner_role = "You are a senior short-form video script planner."
            mode_rules = (
                "- Create between 6 and 12 scenes unless the story is extremely short or long.\n"
                "- Each scene should represent only ONE key moment or action.\n"
                "- narrationText must contain only 1 short sentence whenever possible.\n"
                "- Maximum 8-14 words per narrationText. Never exceed 16 words.\n"
                "- If a sentence is too long, split it into multiple scenes.\n"
                "- Each narration should be readable in about 2-4 seconds.\n"
                "- Avoid long descriptions, unnecessary details, and complex sentence structures.\n"
                "- durationSeconds should match the narration naturally (usually 2-4 seconds).\n"
            )
            input_label = "User idea/script"
            max_output_tokens = None

        prompt = (
            f"{planner_role}\n"
            "Turn the user's input into a JSON array of scenes.\n"
            "Return JSON only.\n"
            "Each scene must have exactly these keys:\n"
            "- sceneIndex\n"
            "- narrationText\n"
            "- imagePrompt\n"
            "- durationSeconds\n"
            "Rules:\n"
            "- Return ONLY valid JSON.\n"
            f"{mode_rules}"
            "- Write narration in a natural conversational style for AI text-to-speech.\n"
            "- imagePrompt must describe exactly what is visible in that scene.\n"
            "- Keep every scene visually different from the previous one.\n"
            "- Keep the story smooth, emotional, and coherent from scene to scene.\n\n"
            "Follow the additional instructions below when writing narrationText and imagePrompt, "
            "but never let them change the required JSON output format.\n"
            f"{instructions_text}\n\n"
            f"{input_label}:\n"
            f"{idea.strip()}\n"
        )

        self.logger(
            f"Calling Gemini text model: {GEMINI_TEXT_MODEL} (mode={video_mode})"
        )
        try:
            response = self.client.models.generate_content(
                model=GEMINI_TEXT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    response_mime_type="application/json",
                    max_output_tokens=max_output_tokens,
                ),
            )
        except Exception:
            self.token_usage.record_unreported_request(
                f"scene_script_{video_mode.lower()}"
            )
            raise
        self.token_usage.record_response(
            response, f"scene_script_{video_mode.lower()}"
        )

        raw_text = extract_model_text(response)
        payload = self._load_scene_json_payload(raw_text)
        scenes = normalize_scenes(payload)
        self.logger(f"Gemini returned {len(scenes)} scenes.")
        return scenes

    def _load_scene_json_payload(self, raw_text: str) -> Any:
        try:
            return load_json_with_common_repairs(raw_text)
        except json.JSONDecodeError as exc:
            self.logger(
                "Gemini returned invalid scene JSON. "
                f"Trying one JSON repair request. Error: {exc}"
            )
            return self._repair_scene_json_with_gemini(raw_text, exc)

    def _repair_scene_json_with_gemini(
        self,
        raw_text: str,
        parse_error: json.JSONDecodeError,
    ) -> Any:
        repair_prompt = (
            "Fix the following invalid JSON into valid JSON only.\n"
            "Do not translate, summarize, add markdown, or change the content.\n"
            "The result must be either a JSON array of scenes or an object with a scenes array.\n"
            "Required scene keys: sceneIndex, narrationText, imagePrompt, durationSeconds.\n"
            f"Parser error: {parse_error}\n\n"
            "Invalid JSON:\n"
            f"{raw_text.strip()}"
        )
        try:
            response = self.client.models.generate_content(
                model=GEMINI_TEXT_MODEL,
                contents=repair_prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                ),
            )
        except Exception:
            self.token_usage.record_unreported_request("scene_json_repair")
            raise
        self.token_usage.record_response(response, "scene_json_repair")
        repaired_text = extract_model_text(response)
        return load_json_with_common_repairs(repaired_text)

    def generate_image(
        self,
        prompt: str,
        model_config: dict[str, str],
        output_path: str | Path,
        reference_images: Sequence[ReferenceImage] = (),
    ) -> Path:
        full_prompt = build_image_prompt(prompt)
        model_name = model_config["id"]
        model_family = model_config["family"]

        if model_family == "gemini_image" or is_gemini_image_model(model_name):
            return retry_with_backoff(
                lambda: self._generate_gemini_image_once(
                    full_prompt,
                    model_name,
                    output_path,
                    reference_images,
                ),
                delays=GEMINI_RETRY_DELAYS,
                logger=self.logger,
                should_retry=is_retryable_gemini_error,
            )

        return retry_with_backoff(
            lambda: self._generate_imagen_image_once(full_prompt, model_name, output_path),
            delays=GEMINI_RETRY_DELAYS,
            logger=self.logger,
            should_retry=is_retryable_gemini_error,
        )

    def _generate_gemini_image_once(
        self,
        full_prompt: str,
        model_name: str,
        output_path: str | Path,
        reference_images: Sequence[ReferenceImage],
    ) -> Path:
        self.logger(f"Generating image with Gemini image model: {model_name}")
        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=build_gemini_image_contents(full_prompt, reference_images),
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio="16:9",
                    ),
                ),
            )
        except Exception:
            self.token_usage.record_unreported_request("gemini_image")
            raise
        self.token_usage.record_response(response, "gemini_image")

        image_obj = extract_first_image_from_generate_content_response(response)
        if image_obj is None:
            response_text = extract_model_text(response)
            if response_text:
                raise ImageGenerationNoOutputError(
                    f"{model_name} returned text instead of image: {response_text[:300]}"
                )
            raise ImageGenerationNoOutputError(f"{model_name} returned no image output.")

        return save_generated_image(image_obj, output_path)

    def _generate_imagen_image_once(
        self,
        full_prompt: str,
        model_name: str,
        output_path: str | Path,
    ) -> Path:
        try:
            self.logger(f"Generating image with Imagen model: {model_name}")
            try:
                response = self.client.models.generate_images(
                    model=model_name,
                    prompt=full_prompt,
                    config=types.GenerateImagesConfig(
                        number_of_images=1,
                        aspect_ratio="16:9",
                        output_mime_type="image/png",
                    ),
                )
            except Exception:
                self.token_usage.record_unreported_request("imagen_image")
                raise
            self.token_usage.record_response(response, "imagen_image")
            generated_images = getattr(response, "generated_images", None) or []
            if not generated_images:
                raise RuntimeError("Imagen returned no images.")

            return save_generated_image(generated_images[0].image, output_path)
        except Exception as exc:
            message = str(exc).lower()
            if "only available on paid plans" in message or "invalid_argument" in message:
                raise RuntimeError(
                    f"{model_name} is not available for the current Google plan/project. "
                    "Use Gemini 3.1 Flash-Lite Image or Gemini 3.1 Flash Image instead."
                ) from exc
            raise


class TtsForFreeClient:
    def __init__(
        self,
        endpoint: str,
        logger: LogFn,
        api_key: str = "",
        speed: float = TTSFORFREE_DEFAULT_SPEED,
        pitch: float = TTSFORFREE_DEFAULT_PITCH,
        volume: float = TTSFORFREE_DEFAULT_VOLUME,
    ) -> None:
        ensure_runtime_dependencies()
        self.session = requests.Session()
        self.logger = logger
        self.connection_id = str(uuid.uuid4())
        self.api_key = (api_key or "").strip()
        self.speed = speed
        self.pitch = pitch
        self.volume = volume
        self.base_url, self.create_url = self._normalize_urls(endpoint)
        self.extra_headers = load_extra_headers()

    def submit_srt_job(self, srt_path: str | Path, voice: str) -> int:
        encrypted_voice = encrypt_voice_token_if_needed(voice)
        cues = load_srt_cues(srt_path)
        if not cues:
            raise RuntimeError("No valid SRT cues found to submit.")

        payload = {
            "connectionId": self.connection_id,
            "blocks": [
                {
                    "texts": cue["text"],
                    "voice": encrypted_voice,
                    "speed": self.speed,
                    "pitch": self.pitch,
                    "volume": self.volume,
                    "startMs": cue["start_ms"],
                    "endMs": cue["end_ms"],
                }
                for cue in cues
            ],
        }

        self.logger(f"Submitting CreateSrt job with {len(cues)} blocks.")
        response = self.session.post(
            self.create_url,
            json=payload,
            headers=self._build_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        response.raise_for_status()
        data = self._parse_json_response(response)
        if int(ci_get(data, "id", default=0)) == -1:
            raise RuntimeError(ci_get(data, "message", default="TTS For Free rejected the job."))

        status = str(ci_get(data, "status", default="")).strip().lower()
        if status in {"error", "failed"}:
            raise RuntimeError(
                ci_get(data, "message", default="TTS For Free failed to create the job.")
            )

        job_id = ci_get(data, "jobId", "id")
        if not isinstance(job_id, (int, float, str)):
            raise RuntimeError("CreateSrt did not return a valid job id.")

        self.logger(f"TTS job created successfully. Job ID: {job_id}")
        return int(job_id)

    def poll_until_completed(
        self,
        job_id: int,
        poll_interval_seconds: int = TTSFORFREE_POLL_INTERVAL_SECONDS,
        timeout_seconds: int = TTSFORFREE_POLL_TIMEOUT_SECONDS,
    ) -> str:
        deadline = time.time() + timeout_seconds
        status_url = self.base_url + TTSFORFREE_STATUS_PATH_TEMPLATE.format(job_id=job_id)

        while time.time() < deadline:
            response = self.session.get(
                status_url,
                headers=self._build_headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = self._parse_json_response(response)

            if int(ci_get(data, "id", default=0)) == -1:
                raise RuntimeError(
                    ci_get(data, "message", default="Status API returned an invalid job.")
                )

            status = str(ci_get(data, "status", default="")).strip().lower()
            audio_url = (
                ci_get(data, "audioUrl")
                or ci_get(data, "data")
                or ci_get(data, "url")
                or ci_get(data, "expectUrl")
            )

            if status in {"success", "completed"}:
                if not audio_url:
                    raise RuntimeError("Job completed but no audio URL was returned.")
                self.logger(f"TTS job {job_id} completed.")
                return str(audio_url)

            if status in {"error", "failed"}:
                raise TtsJobFailedError(
                    ci_get(data, "message", default=f"Job {job_id} failed.")
                )

            self.logger(f"TTS job {job_id} status: {status or 'pending'}")
            time.sleep(poll_interval_seconds)

        raise TimeoutError(f"TTS job {job_id} did not finish within {timeout_seconds} seconds.")

    def download_audio(self, audio_url: str, output_path: str | Path | None = None) -> Path:
        final_path = Path(output_path) if output_path else Path.cwd() / ("audio" + guess_extension_from_url(audio_url))
        return download_file(
            audio_url,
            final_path,
            session=self.session,
            headers=self._build_headers(),
            logger=self.logger,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "GeminiTtsForFreeTool/1.0",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if TTSFORFREE_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {TTSFORFREE_BEARER_TOKEN}"
        if TTSFORFREE_COOKIE:
            headers["Cookie"] = TTSFORFREE_COOKIE
        headers.update(self.extra_headers)
        return headers

    @staticmethod
    def _parse_json_response(response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("TTS For Free returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise RuntimeError("TTS For Free returned an unexpected response shape.")
        return data

    @staticmethod
    def _normalize_urls(endpoint: str) -> tuple[str, str]:
        raw = (endpoint or "").strip()
        if not raw:
            raise RuntimeError("TTS endpoint is required.")

        endpoint_value = raw.rstrip("/")
        lower_value = endpoint_value.lower()

        removable_suffixes = (
            "/docs",
            "/swagger",
            "/swagger/index.html",
            "/swagger/",
        )
        for suffix in removable_suffixes:
            if lower_value.endswith(suffix):
                endpoint_value = endpoint_value[: -len(suffix)]
                lower_value = endpoint_value.lower()
                break

        if lower_value.endswith(TTSFORFREE_CREATE_SRT_PATH.lower()):
            base_url = endpoint_value[: -len(TTSFORFREE_CREATE_SRT_PATH)]
            create_url = endpoint_value
        else:
            base_url = endpoint_value
            create_url = base_url + TTSFORFREE_CREATE_SRT_PATH

        return base_url.rstrip("/"), create_url


class VideoAssembler:
    def __init__(self, logger: LogFn):
        self.logger = logger

    def create_image_video(
        self,
        scenes: Sequence[Scene],
        image_paths: Sequence[Path],
        manifest_path: str | Path,
        output_video_path: str | Path,
    ) -> Path:
        if len(scenes) != len(image_paths):
            raise RuntimeError("The scene list and image list must have the same length.")

        manifest = Path(manifest_path)
        manifest.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        for scene, image_path in zip(scenes, image_paths):
            safe_path = make_safe_ffmpeg_manifest_path(image_path)
            lines.append(f"file '{safe_path}'")
            lines.append(f"duration {max(1, int(scene.durationSeconds))}")

        if image_paths:
            lines.append(f"file '{make_safe_ffmpeg_manifest_path(image_paths[-1])}'")

        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        vf = (
            f"fps={VIDEO_FPS},"
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuv420p"
        )
        run_ffmpeg(
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output_video_path),
            ],
            logger=self.logger,
        )
        return Path(output_video_path)

    def merge_video_audio_and_subtitles(
        self,
        image_video_path: str | Path,
        audio_path: str | Path,
        srt_path: str | Path,
        final_output_path: str | Path,
    ) -> Path:
        image_duration = probe_media_duration_seconds(image_video_path)
        audio_duration = probe_media_duration_seconds(audio_path)
        duration_target = max(audio_duration, 0.1)
        extend_seconds = max(0.0, audio_duration - image_duration)

        subtitle_filter_path = ffmpeg_escape_subtitle_path(srt_path)
        filter_chain = []
        if extend_seconds > 0.01:
            filter_chain.append(f"tpad=stop_mode=clone:stop_duration={extend_seconds:.3f}")
        filter_chain.append(f"subtitles='{subtitle_filter_path}'")
        filter_chain.append("format=yuv420p")

        run_ffmpeg(
            [
                "-y",
                "-i",
                str(image_video_path),
                "-i",
                str(audio_path),
                "-vf",
                ",".join(filter_chain),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-t",
                f"{duration_target:.3f}",
                str(final_output_path),
            ],
            logger=self.logger,
        )

        return Path(final_output_path)


class GeminiTtsForFreeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Gemini + TTS For Free Video Tool")
        self.root.geometry("1180x940")
        self.root.minsize(980, 760)
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self._save_settings_after_id: str | None = None
        self._loading_settings = True
        self.job_cards: list[VideoJobCard] = []

        self.api_key_var = tk.StringVar()
        self.tts_endpoint_var = tk.StringVar(value=DEFAULT_TTS_ENDPOINT)
        self.tts_api_key_var = tk.StringVar(value=TTSFORFREE_API_KEY)
        self.image_model_var = tk.StringVar(value=list(IMAGE_MODEL_OPTIONS.keys())[0])
        self.parallel_image_requests_var = tk.StringVar(value=str(IMAGE_GENERATION_MAX_WORKERS))
        self.batch_mode_var = tk.StringVar(value=BATCH_MODE_SEQUENTIAL)
        self.max_parallel_videos_var = tk.StringVar(value="2")
        self.shared_reference_image_paths: list[str] = []
        self.shared_reference_images_var = tk.StringVar(
            value=summarize_reference_image_paths([])
        )
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self._load_settings()
        self._bind_settings_autosave()
        self._loading_settings = False
        self.root.after(150, self._flush_logs)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=3)
        frame.rowconfigure(8, weight=2)

        credentials = ttk.LabelFrame(frame, text="API configuration", padding=8)
        credentials.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        credentials.columnconfigure(1, weight=1)
        ttk.Label(credentials, text="Gemini / Google API Key").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=2
        )
        ttk.Entry(credentials, textvariable=self.api_key_var, show="*").grid(
            row=0, column=1, sticky="ew", pady=2
        )
        ttk.Label(credentials, text="TTS For Free Endpoint").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=2
        )
        ttk.Entry(credentials, textvariable=self.tts_endpoint_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Label(credentials, text="TTS API Key (X-API-Key)").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=2
        )
        ttk.Entry(credentials, textvariable=self.tts_api_key_var, show="*").grid(
            row=2, column=1, sticky="ew", pady=2
        )

        generation_config = ttk.LabelFrame(frame, text="Batch configuration", padding=8)
        generation_config.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        generation_config.columnconfigure(1, weight=1)
        generation_config.columnconfigure(5, weight=1)
        ttk.Label(generation_config, text="Image Model").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        ttk.Combobox(
            generation_config,
            textvariable=self.image_model_var,
            values=list(IMAGE_MODEL_OPTIONS.keys()),
            state="readonly",
            width=48,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(generation_config, text="Image threads / video").grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        ttk.Spinbox(
            generation_config,
            textvariable=self.parallel_image_requests_var,
            from_=1,
            to=12,
            width=6,
        ).grid(row=0, column=3, sticky="w", padx=(0, 12))
        ttk.Label(generation_config, text="Run mode").grid(
            row=0, column=4, sticky="w", padx=(0, 6)
        )
        ttk.Combobox(
            generation_config,
            textvariable=self.batch_mode_var,
            values=BATCH_MODE_OPTIONS,
            state="readonly",
            width=11,
        ).grid(row=0, column=5, sticky="w", padx=(0, 12))
        ttk.Label(generation_config, text="Video threads").grid(
            row=0, column=6, sticky="w", padx=(0, 6)
        )
        ttk.Spinbox(
            generation_config,
            textvariable=self.max_parallel_videos_var,
            from_=1,
            to=12,
            width=6,
        ).grid(row=0, column=7, sticky="w")
        ttk.Label(
            generation_config,
            text=(
                "Parallel mode can use up to Video threads × Image threads API calls "
                "at the same time."
            ),
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))

        shared_frame = ttk.LabelFrame(
            frame,
            text="Shared prompt and shared reference images",
            padding=8,
        )
        shared_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        shared_frame.columnconfigure(1, weight=1)
        ttk.Label(shared_frame, text="Shared Prompt").grid(
            row=0, column=0, sticky="nw", padx=(0, 8)
        )
        self.shared_prompt_text = scrolledtext.ScrolledText(
            shared_frame, height=4, wrap="word"
        )
        self.shared_prompt_text.grid(row=0, column=1, columnspan=4, sticky="ew")
        self.shared_prompt_text.bind("<KeyRelease>", self._schedule_settings_save)
        self.shared_prompt_text.bind("<FocusOut>", self._schedule_settings_save)

        ttk.Label(shared_frame, text="Shared Images").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        ttk.Button(
            shared_frame,
            text="Add Images...",
            command=self._choose_shared_reference_images,
        ).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Button(
            shared_frame,
            text="Clear",
            command=lambda: self._set_shared_reference_image_paths([]),
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(shared_frame, textvariable=self.shared_reference_images_var).grid(
            row=1,
            column=3,
            columnspan=2,
            sticky="w",
            padx=(10, 0),
            pady=(6, 0),
        )

        jobs_header = ttk.Frame(frame)
        jobs_header.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(jobs_header, text="Videos in this batch").pack(side="left")
        ttk.Button(
            jobs_header,
            text="Add Blank Video",
            command=self._add_job_card,
        ).pack(side="right")

        jobs_host = ttk.Frame(frame)
        jobs_host.grid(row=5, column=0, sticky="nsew", pady=(0, 8))
        jobs_host.rowconfigure(0, weight=1)
        jobs_host.columnconfigure(0, weight=1)
        self.jobs_canvas = tk.Canvas(jobs_host, highlightthickness=0)
        jobs_scrollbar = ttk.Scrollbar(
            jobs_host, orient="vertical", command=self.jobs_canvas.yview
        )
        self.jobs_canvas.configure(yscrollcommand=jobs_scrollbar.set)
        self.jobs_canvas.grid(row=0, column=0, sticky="nsew")
        jobs_scrollbar.grid(row=0, column=1, sticky="ns")
        self.jobs_container = ttk.Frame(self.jobs_canvas)
        self.jobs_canvas_window = self.jobs_canvas.create_window(
            (0, 0), window=self.jobs_container, anchor="nw"
        )
        self.jobs_container.columnconfigure(0, weight=1)
        self.jobs_container.bind(
            "<Configure>",
            lambda _event: self.jobs_canvas.configure(
                scrollregion=self.jobs_canvas.bbox("all")
            ),
        )
        self.jobs_canvas.bind(
            "<Configure>",
            lambda event: self.jobs_canvas.itemconfigure(
                self.jobs_canvas_window, width=event.width
            ),
        )

        action_frame = ttk.Frame(frame)
        action_frame.grid(row=6, column=0, sticky="ew", pady=(0, 8))

        self.generate_button = ttk.Button(
            action_frame, text="Generate Batch", command=self._start_generation
        )
        self.generate_button.pack(side="left")
        ttk.Button(action_frame, text="History", command=self._open_history_window).pack(
            side="left",
            padx=(8, 0),
        )
        ttk.Label(action_frame, textvariable=self.status_var).pack(side="right")

        output_frame = ttk.Frame(frame)
        output_frame.grid(row=7, column=0, sticky="ew", pady=(0, 8))
        output_frame.columnconfigure(1, weight=1)
        ttk.Label(output_frame, text="Completed Outputs").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(output_frame, textvariable=self.output_var, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )

        log_frame = ttk.LabelFrame(frame, text="Realtime Log", padding=6)
        log_frame.grid(row=8, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, wrap="word", state="disabled"
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _add_job_card(self, initial: dict[str, Any] | None = None) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Wait for the current batch before adding videos.")
            return
        initial = initial or {}
        card_frame = ttk.LabelFrame(self.jobs_container, padding=8)
        card_frame.grid(
            row=len(self.job_cards), column=0, sticky="ew", padx=(0, 4), pady=(0, 8)
        )
        card_frame.columnconfigure(1, weight=1)

        voice_var = tk.StringVar(
            value=str(initial.get("voice", DEFAULT_VOICE_OPTIONS[0])).strip()
            or DEFAULT_VOICE_OPTIONS[0]
        )
        initial_video_mode = str(
            initial.get("video_mode", VIDEO_MODE_SHORT)
        ).strip()
        if initial_video_mode not in VIDEO_MODE_OPTIONS:
            initial_video_mode = VIDEO_MODE_SHORT
        video_mode_var = tk.StringVar(value=initial_video_mode)
        use_shared_var = tk.BooleanVar(
            value=bool(initial.get("use_shared_reference_images", True))
        )
        reference_paths_value = initial.get("reference_image_paths", [])
        reference_paths = normalize_reference_image_paths(
            reference_paths_value if isinstance(reference_paths_value, list) else []
        )
        reference_summary_var = tk.StringVar(
            value=summarize_reference_image_paths(reference_paths)
        )
        status_var = tk.StringVar(value="Ready")
        token_usage_var = tk.StringVar(value=format_token_usage(None))

        ttk.Label(card_frame, text="Voice").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 5)
        )
        ttk.Combobox(
            card_frame,
            textvariable=voice_var,
            values=DEFAULT_VOICE_OPTIONS,
            state="normal",
        ).grid(row=0, column=1, sticky="ew", pady=(0, 5))

        header_actions = ttk.Frame(card_frame)
        header_actions.grid(row=0, column=2, sticky="e", padx=(10, 0), pady=(0, 5))

        ttk.Label(card_frame, text="Video Prompt").grid(
            row=1, column=0, sticky="nw", padx=(0, 8), pady=(0, 5)
        )
        prompt_text = scrolledtext.ScrolledText(card_frame, height=3, wrap="word")
        prompt_text.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 5))
        prompt_text.insert("1.0", str(initial.get("video_prompt", "")))

        ttk.Label(card_frame, text="Idea / Script").grid(
            row=2, column=0, sticky="nw", padx=(0, 8), pady=(0, 5)
        )
        idea_text = scrolledtext.ScrolledText(card_frame, height=6, wrap="word")
        idea_text.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(0, 5))
        idea_text.insert("1.0", str(initial.get("idea", "")))

        ttk.Label(card_frame, text="Own Images").grid(
            row=3, column=0, sticky="w", padx=(0, 8)
        )
        image_actions = ttk.Frame(card_frame)
        image_actions.grid(row=3, column=1, columnspan=2, sticky="ew")
        image_actions.columnconfigure(3, weight=1)

        card = VideoJobCard(
            job_id=uuid.uuid4().hex,
            frame=card_frame,
            video_mode_var=video_mode_var,
            voice_var=voice_var,
            video_prompt_text=prompt_text,
            idea_text=idea_text,
            use_shared_references_var=use_shared_var,
            reference_image_paths=reference_paths,
            reference_images_var=reference_summary_var,
            status_var=status_var,
            token_usage_var=token_usage_var,
        )
        self.job_cards.append(card)

        ttk.Label(header_actions, text="Mode").pack(side="left", padx=(0, 5))
        ttk.Combobox(
            header_actions,
            textvariable=video_mode_var,
            values=VIDEO_MODE_OPTIONS,
            state="readonly",
            width=7,
        ).pack(side="left", padx=(0, 10))
        ttk.Button(
            header_actions,
            text="Clone",
            command=lambda current=card: self._clone_job_card(current),
        ).pack(side="left")
        ttk.Button(
            header_actions,
            text="Remove",
            command=lambda current=card: self._remove_job_card(current),
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            image_actions,
            text="Add Images...",
            command=lambda current=card: self._choose_job_reference_images(current),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            image_actions,
            text="Clear",
            command=lambda current=card: self._set_job_reference_image_paths(current, []),
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Checkbutton(
            image_actions,
            text="Use shared images (unchecked = use own images)",
            variable=use_shared_var,
            command=self._schedule_settings_save,
        ).grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(image_actions, textvariable=reference_summary_var).grid(
            row=0, column=3, sticky="w", padx=(10, 0)
        )
        ttk.Label(card_frame, textvariable=token_usage_var).grid(
            row=4, column=1, sticky="w", pady=(5, 0)
        )
        ttk.Label(card_frame, textvariable=status_var).grid(
            row=4, column=2, sticky="e", pady=(5, 0)
        )

        voice_var.trace_add("write", self._schedule_settings_save)
        video_mode_var.trace_add("write", self._schedule_settings_save)
        prompt_text.bind("<KeyRelease>", self._schedule_settings_save)
        prompt_text.bind("<FocusOut>", self._schedule_settings_save)
        idea_text.bind("<KeyRelease>", self._schedule_settings_save)
        idea_text.bind("<FocusOut>", self._schedule_settings_save)
        self._renumber_job_cards()
        self._schedule_settings_save()

    def _renumber_job_cards(self) -> None:
        for index, card in enumerate(self.job_cards, start=1):
            card.frame.configure(text=f"Video {index}")
            card.frame.grid_configure(row=index - 1)

    def _card_payload(self, card: VideoJobCard) -> dict[str, Any]:
        return {
            "voice": card.voice_var.get().strip(),
            "video_mode": card.video_mode_var.get().strip(),
            "video_prompt": card.video_prompt_text.get("1.0", "end").strip(),
            "idea": card.idea_text.get("1.0", "end").strip(),
            "use_shared_reference_images": card.use_shared_references_var.get(),
            "reference_image_paths": list(card.reference_image_paths),
        }

    def _clone_job_card(self, card: VideoJobCard) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Wait for the current batch before cloning videos.")
            return
        self._add_job_card(self._card_payload(card))
        self.jobs_canvas.yview_moveto(1.0)

    def _remove_job_card(self, card: VideoJobCard) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Wait for the current batch before removing videos.")
            return
        if len(self.job_cards) <= 1:
            messagebox.showinfo("Keep One Video", "The batch must contain at least one video.")
            return
        if card not in self.job_cards:
            return
        self.job_cards.remove(card)
        card.frame.destroy()
        self._renumber_job_cards()
        self._schedule_settings_save()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _build_settings_payload(self) -> dict[str, Any]:
        first_card = self.job_cards[0] if self.job_cards else None
        return {
            "gemini_api_key": self.api_key_var.get().strip(),
            "tts_endpoint": self.tts_endpoint_var.get().strip(),
            "tts_api_key": self.tts_api_key_var.get().strip(),
            "voice": first_card.voice_var.get().strip() if first_card else "",
            "image_model_label": self.image_model_var.get().strip(),
            "parallel_image_requests": positive_int_or_default(
                self.parallel_image_requests_var.get(),
                IMAGE_GENERATION_MAX_WORKERS,
            ),
            "batch_mode": self.batch_mode_var.get().strip(),
            "max_parallel_videos": positive_int_or_default(
                self.max_parallel_videos_var.get(), 2
            ),
            "shared_prompt": self.shared_prompt_text.get("1.0", "end").strip(),
            "shared_reference_image_paths": list(self.shared_reference_image_paths),
            # Retain the old key so settings remain readable by older copies.
            "reference_image_paths": list(self.shared_reference_image_paths),
            "video_jobs": [self._card_payload(card) for card in self.job_cards],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _load_settings(self) -> None:
        if not SETTINGS_PATH.exists():
            self._add_job_card()
            return

        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError("Settings file must be a JSON object.")

            self.api_key_var.set(str(data.get("gemini_api_key", self.api_key_var.get())).strip())
            self.tts_endpoint_var.set(
                str(data.get("tts_endpoint", self.tts_endpoint_var.get())).strip()
            )
            self.tts_api_key_var.set(
                str(data.get("tts_api_key", self.tts_api_key_var.get())).strip()
            )
            saved_model_label = str(data.get("image_model_label", "")).strip()
            if saved_model_label in IMAGE_MODEL_OPTIONS:
                self.image_model_var.set(saved_model_label)

            self.parallel_image_requests_var.set(
                str(
                    positive_int_or_default(
                        data.get("parallel_image_requests", IMAGE_GENERATION_MAX_WORKERS),
                        IMAGE_GENERATION_MAX_WORKERS,
                    )
                )
            )

            saved_batch_mode = str(data.get("batch_mode", BATCH_MODE_SEQUENTIAL)).strip()
            if saved_batch_mode in BATCH_MODE_OPTIONS:
                self.batch_mode_var.set(saved_batch_mode)
            self.max_parallel_videos_var.set(
                str(positive_int_or_default(data.get("max_parallel_videos", 2), 2))
            )

            self.shared_prompt_text.delete("1.0", "end")
            self.shared_prompt_text.insert("1.0", str(data.get("shared_prompt", "")))

            reference_image_paths = data.get(
                "shared_reference_image_paths", data.get("reference_image_paths", [])
            )
            if isinstance(reference_image_paths, list):
                self._set_shared_reference_image_paths(
                    [str(path) for path in reference_image_paths],
                    save=False,
                )

            saved_jobs = data.get("video_jobs", [])
            if isinstance(saved_jobs, list):
                for saved_job in saved_jobs:
                    if isinstance(saved_job, dict):
                        self._add_job_card(saved_job)

            if not self.job_cards:
                self._add_job_card(
                    {
                        "voice": str(data.get("voice", DEFAULT_VOICE_OPTIONS[0])).strip(),
                        "use_shared_reference_images": True,
                    }
                )

            self.log(f"Loaded saved settings from {SETTINGS_PATH.name}")
        except Exception as exc:
            self.log(f"Could not load saved settings: {exc}")
            if not self.job_cards:
                self._add_job_card()

        if not self.job_cards:
            self._add_job_card()

    def _save_settings(self, log_result: bool = False) -> None:
        payload = self._build_settings_payload()
        SETTINGS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if log_result:
            self.log(f"Saved settings to {SETTINGS_PATH.name}")

    def _schedule_settings_save(self, *_args: Any) -> None:
        if self._loading_settings:
            return
        if self._save_settings_after_id:
            try:
                self.root.after_cancel(self._save_settings_after_id)
            except tk.TclError:
                pass

        self._save_settings_after_id = self.root.after(500, self._autosave_settings)

    def _autosave_settings(self) -> None:
        self._save_settings_after_id = None
        try:
            self._save_settings()
        except Exception as exc:
            self.log(f"Could not save settings: {exc}")

    def _bind_settings_autosave(self) -> None:
        for variable in (
            self.api_key_var,
            self.tts_endpoint_var,
            self.tts_api_key_var,
            self.image_model_var,
            self.parallel_image_requests_var,
            self.batch_mode_var,
            self.max_parallel_videos_var,
        ):
            variable.trace_add("write", self._schedule_settings_save)

    def _handle_close(self) -> None:
        try:
            self._save_settings(log_result=True)
        except Exception as exc:
            self.log(f"Could not save settings on exit: {exc}")
        self.root.destroy()

    def _set_shared_reference_image_paths(
        self, paths: Sequence[str], save: bool = True
    ) -> None:
        self.shared_reference_image_paths = normalize_reference_image_paths(paths)
        self.shared_reference_images_var.set(
            summarize_reference_image_paths(self.shared_reference_image_paths)
        )

        if save:
            try:
                self._save_settings()
            except Exception as exc:
                self.log(f"Could not save reference image settings: {exc}")

    def _choose_shared_reference_images(self) -> None:
        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Select shared reference images",
            filetypes=REFERENCE_IMAGE_FILETYPES,
        )
        if not selected_paths:
            return

        before_count = len(self.shared_reference_image_paths)
        self._set_shared_reference_image_paths(
            [*self.shared_reference_image_paths, *selected_paths]
        )

        if len(self.shared_reference_image_paths) == before_count:
            messagebox.showinfo(
                "No New Images",
                "No new supported reference images were added.",
            )

    def _set_job_reference_image_paths(
        self, card: VideoJobCard, paths: Sequence[str], save: bool = True
    ) -> None:
        card.reference_image_paths = normalize_reference_image_paths(paths)
        card.reference_images_var.set(
            summarize_reference_image_paths(card.reference_image_paths)
        )
        if save:
            self._schedule_settings_save()

    def _choose_job_reference_images(self, card: VideoJobCard) -> None:
        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Select reference images for this video",
            filetypes=REFERENCE_IMAGE_FILETYPES,
        )
        if not selected_paths:
            return
        before_count = len(card.reference_image_paths)
        self._set_job_reference_image_paths(
            card, [*card.reference_image_paths, *selected_paths]
        )
        if len(card.reference_image_paths) == before_count:
            messagebox.showinfo(
                "No New Images",
                "No new supported reference images were added.",
            )

    def _flush_logs(self) -> None:
        while not self.log_queue.empty():
            message = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(150, self._flush_logs)

    def _open_history_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Generation History")
        window.geometry("1120x720")
        window.minsize(860, 520)

        container = ttk.Frame(window, padding=12)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        paned = ttk.PanedWindow(container, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew")

        left_frame = ttk.Frame(paned)
        right_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)
        paned.add(right_frame, weight=3)

        left_frame.rowconfigure(1, weight=1)
        left_frame.columnconfigure(0, weight=1)
        right_frame.rowconfigure(1, weight=1)
        right_frame.columnconfigure(0, weight=1)

        ttk.Label(left_frame, text="Generated Videos").grid(row=0, column=0, sticky="w", pady=(0, 6))
        history_list = tk.Listbox(left_frame, exportselection=False, width=42)
        history_list.grid(row=1, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(left_frame, orient="vertical", command=history_list.yview)
        list_scroll.grid(row=1, column=1, sticky="ns")
        history_list.configure(yscrollcommand=list_scroll.set)

        ttk.Label(right_frame, text="Details / Copyable Script").grid(
            row=0,
            column=0,
            sticky="w",
            pady=(0, 6),
        )
        detail_text = scrolledtext.ScrolledText(right_frame, wrap="word")
        detail_text.grid(row=1, column=0, sticky="nsew")

        button_frame = ttk.Frame(right_frame)
        button_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        status_var = tk.StringVar(value="")
        ttk.Label(right_frame, textvariable=status_var).grid(row=3, column=0, sticky="w", pady=(6, 0))

        entries: list[dict[str, Any]] = []

        def set_detail_text(value: str) -> None:
            detail_text.delete("1.0", "end")
            detail_text.insert("1.0", value)

        def format_history_label(entry: dict[str, Any]) -> str:
            created_at = str(entry.get("created_at", "")).replace("T", " ")[:19]
            run_id = str(entry.get("run_id", "")).strip() or "(no run id)"
            final_video_path = str(entry.get("final_video_path", "")).strip()
            missing_suffix = ""
            if final_video_path and not Path(final_video_path).exists():
                missing_suffix = " | missing video"
            return f"{created_at} | {run_id}{missing_suffix}"

        def selected_entry(show_warning: bool = True) -> dict[str, Any] | None:
            selection = history_list.curselection()
            if not selection:
                if show_warning:
                    status_var.set("Select a history item first.")
                return None
            return entries[selection[0]]

        def render_selected(_event: Any = None) -> None:
            entry = selected_entry(show_warning=False)
            if entry is None:
                return
            set_detail_text(format_history_entry_text(entry))
            status_var.set("")

        def refresh_history() -> None:
            nonlocal entries
            entries = load_generation_history()
            history_list.delete(0, "end")

            if not entries:
                set_detail_text(
                    "No generated video history found yet.\n\n"
                    "After a successful generation, it will show up here automatically."
                )
                status_var.set("")
                return

            for entry in entries:
                history_list.insert("end", format_history_label(entry))

            history_list.selection_set(0)
            history_list.activate(0)
            render_selected()

        def open_selected_path(field_name: str, title: str) -> None:
            entry = selected_entry()
            if entry is None:
                return

            path = str(entry.get(field_name, "")).strip()
            if not path:
                status_var.set(f"No {title.lower()} path found for this history item.")
                return

            try:
                open_path_with_default_app(path)
                status_var.set(f"Opened {title.lower()}.")
            except Exception as exc:
                messagebox.showerror("Open Failed", str(exc), parent=window)

        def copy_visible_text() -> None:
            text = detail_text.get("1.0", "end-1c")
            if not text.strip():
                status_var.set("Nothing to copy yet.")
                return

            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            status_var.set("Copied visible text to clipboard.")

        ttk.Button(
            button_frame,
            text="Open Video",
            command=lambda: open_selected_path("final_video_path", "Video"),
        ).pack(side="left")
        ttk.Button(
            button_frame,
            text="Open Folder",
            command=lambda: open_selected_path("output_dir", "Output Folder"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(button_frame, text="Copy Text", command=copy_visible_text).pack(
            side="left",
            padx=(8, 0),
        )
        ttk.Button(button_frame, text="Refresh", command=refresh_history).pack(
            side="left",
            padx=(8, 0),
        )

        history_list.bind("<<ListboxSelect>>", render_selected)
        refresh_history()

    def _start_generation(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "A video batch is already running.")
            return

        api_key = self.api_key_var.get().strip()
        endpoint = self.tts_endpoint_var.get().strip()
        tts_api_key = self.tts_api_key_var.get().strip()
        image_model_label = self.image_model_var.get().strip()
        parallel_image_requests = self.parallel_image_requests_var.get().strip()
        batch_mode = self.batch_mode_var.get().strip()
        shared_prompt = self.shared_prompt_text.get("1.0", "end").strip()
        shared_reference_image_paths = normalize_reference_image_paths(
            self.shared_reference_image_paths
        )

        if not api_key:
            messagebox.showerror("Missing API Key", "Please enter your Gemini / Google API key.")
            return
        if not endpoint:
            messagebox.showerror("Missing Endpoint", "Please enter the TTS For Free API endpoint.")
            return
        if not tts_api_key and not TTSFORFREE_BEARER_TOKEN and not TTSFORFREE_COOKIE:
            messagebox.showerror(
                "Missing TTS API Key",
                "Please enter the TTS For Free API key. This tool sends it as X-API-Key.",
            )
            return
        if image_model_label not in IMAGE_MODEL_OPTIONS:
            messagebox.showerror("Invalid Image Model", "Please select a valid image model.")
            return
        if batch_mode not in BATCH_MODE_OPTIONS:
            messagebox.showerror("Invalid Run Mode", "Please select Sequential or Parallel.")
            return
        try:
            max_image_workers = parse_positive_int(
                parallel_image_requests,
                "Parallel Image Requests",
            )
            max_video_workers = parse_positive_int(
                self.max_parallel_videos_var.get(),
                "Video Threads",
            )
        except ValueError as exc:
            messagebox.showerror("Invalid Parallel Requests", str(exc))
            return

        if shared_reference_image_paths != self.shared_reference_image_paths:
            self._set_shared_reference_image_paths(
                shared_reference_image_paths, save=False
            )

        requests_to_run: list[VideoJobRequest] = []
        for position, card in enumerate(self.job_cards, start=1):
            voice = card.voice_var.get().strip()
            video_mode = card.video_mode_var.get().strip()
            idea = card.idea_text.get("1.0", "end").strip()
            video_prompt = card.video_prompt_text.get("1.0", "end").strip()
            if not voice:
                messagebox.showerror(
                    "Missing Voice", f"Video {position} does not have a voice."
                )
                return
            if video_mode not in VIDEO_MODE_OPTIONS:
                messagebox.showerror(
                    "Invalid Video Mode",
                    f"Video {position} must use Short or Long mode.",
                )
                return
            if not idea:
                messagebox.showerror(
                    "Missing Script", f"Video {position} does not have an idea / script."
                )
                return

            own_reference_paths = normalize_reference_image_paths(
                card.reference_image_paths
            )
            if own_reference_paths != card.reference_image_paths:
                self._set_job_reference_image_paths(
                    card, own_reference_paths, save=False
                )
            use_shared = card.use_shared_references_var.get()
            selected_reference_paths = (
                shared_reference_image_paths if use_shared else own_reference_paths
            )
            requests_to_run.append(
                VideoJobRequest(
                    job_id=card.job_id,
                    position=position,
                    video_mode=video_mode,
                    idea=idea,
                    voice=voice,
                    video_prompt=video_prompt,
                    use_shared_reference_images=use_shared,
                    reference_image_paths=tuple(selected_reference_paths),
                )
            )

        if not requests_to_run:
            messagebox.showerror("Empty Batch", "Add at least one video to the batch.")
            return

        self.output_var.set("")
        self.status_var.set(f"Running 0/{len(requests_to_run)}...")
        for card in self.job_cards:
            card.status_var.set("Queued")
            card.token_usage_var.set("Tokens | counting...")
        self.generate_button.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._save_settings(log_result=True)

        self.worker_thread = threading.Thread(
            target=self._run_batch,
            args=(
                api_key,
                endpoint,
                tts_api_key,
                image_model_label,
                max_image_workers,
                batch_mode,
                max_video_workers,
                shared_prompt,
                tuple(requests_to_run),
            ),
            daemon=True,
        )
        self.worker_thread.start()

    def _set_job_status(self, job_id: str, status: str) -> None:
        for card in self.job_cards:
            if card.job_id == job_id:
                card.status_var.set(status)
                return

    def _set_job_token_usage(self, job_id: str, usage: dict[str, Any]) -> None:
        for card in self.job_cards:
            if card.job_id == job_id:
                card.token_usage_var.set(format_token_usage(usage))
                return

    def _run_batch(
        self,
        api_key: str,
        endpoint: str,
        tts_api_key: str,
        image_model_label: str,
        max_image_workers: int,
        batch_mode: str,
        max_video_workers: int,
        shared_prompt: str,
        jobs: Sequence[VideoJobRequest],
    ) -> None:
        batch_started_at = time.time()
        outputs: dict[int, Path] = {}
        errors: dict[int, str] = {}
        token_trackers = {job.job_id: TokenUsageTracker() for job in jobs}

        def record_success(job: VideoJobRequest, output_path: Path) -> None:
            outputs[job.position] = output_path
            completed_count = len(outputs) + len(errors)
            usage = token_trackers[job.job_id].snapshot()
            self.root.after(0, self._set_job_status, job.job_id, "Completed")
            self.root.after(0, self._set_job_token_usage, job.job_id, usage)
            self.root.after(
                0,
                self.status_var.set,
                f"Running {completed_count}/{len(jobs)}...",
            )

        def record_failure(job: VideoJobRequest, exc: Exception) -> None:
            errors[job.position] = str(exc)
            completed_count = len(outputs) + len(errors)
            self.log(f"[Video {job.position}] ERROR: {exc}")
            self.log(traceback.format_exc().strip())
            usage = token_trackers[job.job_id].snapshot()
            self.root.after(0, self._set_job_status, job.job_id, f"Failed: {exc}")
            self.root.after(0, self._set_job_token_usage, job.job_id, usage)
            self.root.after(
                0,
                self.status_var.set,
                f"Running {completed_count}/{len(jobs)}...",
            )

        try:
            ensure_runtime_dependencies()
            ensure_ffmpeg_installed()
            video_worker_count = (
                1
                if batch_mode == BATCH_MODE_SEQUENTIAL
                else min(max_video_workers, max(1, len(jobs)))
            )
            self.log(
                f"Starting batch with {len(jobs)} video(s), mode={batch_mode}, "
                f"video workers={video_worker_count}, image workers/video={max_image_workers}."
            )

            if video_worker_count == 1:
                for job in jobs:
                    self.root.after(0, self._set_job_status, job.job_id, "Running")
                    try:
                        output_path = self._run_single_video(
                            api_key=api_key,
                            endpoint=endpoint,
                            tts_api_key=tts_api_key,
                            image_model_label=image_model_label,
                            max_image_workers=max_image_workers,
                            shared_prompt=shared_prompt,
                            job=job,
                            token_usage=token_trackers[job.job_id],
                        )
                        record_success(job, output_path)
                    except Exception as exc:
                        record_failure(job, exc)
            else:
                with ThreadPoolExecutor(max_workers=video_worker_count) as executor:
                    future_to_job = {}
                    for job in jobs:
                        self.root.after(0, self._set_job_status, job.job_id, "Running")
                        future = executor.submit(
                            self._run_single_video,
                            api_key,
                            endpoint,
                            tts_api_key,
                            image_model_label,
                            max_image_workers,
                            shared_prompt,
                            job,
                            token_trackers[job.job_id],
                        )
                        future_to_job[future] = job

                    for future in as_completed(future_to_job):
                        job = future_to_job[future]
                        try:
                            record_success(job, future.result())
                        except Exception as exc:
                            record_failure(job, exc)

            ordered_outputs = [str(outputs[index]) for index in sorted(outputs)]
            elapsed = time.time() - batch_started_at
            self.log(
                f"Batch finished in {elapsed:.1f}s: "
                f"{len(outputs)} completed, {len(errors)} failed."
            )
            self.root.after(
                0,
                self._finish_batch_ui,
                ordered_outputs,
                errors,
                len(jobs),
                elapsed,
            )
        except Exception as exc:
            self.log("BATCH ERROR: " + str(exc))
            self.log(traceback.format_exc().strip())
            for job in jobs:
                self.root.after(0, self._set_job_status, job.job_id, "Batch failed")
                self.root.after(
                    0,
                    self._set_job_token_usage,
                    job.job_id,
                    token_trackers[job.job_id].snapshot(),
                )
            self.root.after(0, self.status_var.set, "Batch failed")
            error_message = str(exc)
            self.root.after(
                0,
                lambda message=error_message: messagebox.showerror(
                    "Batch Failed", message
                ),
            )
        finally:
            self.root.after(0, lambda: self.generate_button.configure(state="normal"))

    def _finish_batch_ui(
        self,
        output_paths: Sequence[str],
        errors: dict[int, str],
        total_jobs: int,
        elapsed: float,
    ) -> None:
        self.output_var.set(" ; ".join(output_paths))
        completed = len(output_paths)
        if errors:
            self.status_var.set(f"Finished: {completed}/{total_jobs} completed")
            error_lines = [
                f"Video {position}: {message}" for position, message in sorted(errors.items())
            ]
            messagebox.showwarning(
                "Batch Finished With Errors",
                f"Completed {completed}/{total_jobs} video(s) in {elapsed:.1f}s.\n\n"
                + "\n".join(error_lines[:8]),
            )
        else:
            self.status_var.set(f"Completed {completed}/{total_jobs}")
            messagebox.showinfo(
                "Batch Completed",
                f"Created {completed} video(s) in {elapsed:.1f}s.",
            )

    def _run_single_video(
        self,
        api_key: str,
        endpoint: str,
        tts_api_key: str,
        image_model_label: str,
        max_image_workers: int,
        shared_prompt: str,
        job: VideoJobRequest,
        token_usage: TokenUsageTracker,
    ) -> Path:
        started_at = time.time()
        logger: LogFn = lambda message: self.log(f"[Video {job.position}] {message}")
        logger("Starting generation pipeline.")

        paths = build_run_paths()
        run_id = str(paths["run_id"])
        output_dir = paths["output_dir"]
        temp_dir = paths["temp_dir"]
        images_dir = paths["images_dir"]

        scenes_json_path = temp_dir / "scenes.json"
        srt_path = temp_dir / "script.srt"
        concat_manifest_path = temp_dir / "images_concat.txt"
        image_video_path = temp_dir / "image_video.mp4"

        audio_path = temp_dir / "tts_audio.mp3"
        final_output_path = output_dir / "final_video.mp4"
        token_usage_path = output_dir / "token_usage.json"

        def save_token_usage() -> dict[str, Any]:
            usage = token_usage.snapshot()
            payload = {
                "run_id": run_id,
                "batch_position": job.position,
                "video_mode": job.video_mode,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "text_model": GEMINI_TEXT_MODEL,
                "image_model_label": image_model_label,
                "image_model_id": IMAGE_MODEL_OPTIONS[image_model_label]["id"],
                **usage,
            }
            token_usage_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return usage

        logger(f"Temp folder: {temp_dir}")
        logger(f"Output folder: {output_dir}")

        gemini_client = GeminiVideoClient(
            api_key=api_key,
            logger=logger,
            token_usage=token_usage,
        )
        try:
            scenes = gemini_client.generate_scene_script(
                job.idea,
                shared_prompt=shared_prompt,
                video_prompt=job.video_prompt,
                video_mode=job.video_mode,
            )
        except Exception:
            save_token_usage()
            raise
        scenes_json_path.write_text(
            json.dumps([scene.to_dict() for scene in scenes], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger(f"Saved scene JSON: {scenes_json_path}")

        image_model_config = IMAGE_MODEL_OPTIONS[image_model_label]
        reference_images = load_reference_images(job.reference_image_paths)
        reference_source = (
            "shared" if job.use_shared_reference_images else "video-specific"
        )
        if reference_images and (
            image_model_config["family"] == "gemini_image"
            or is_gemini_image_model(image_model_config["id"])
        ):
            logger(
                f"Using {len(reference_images)} {reference_source} reference image(s) "
                "for Gemini image generation."
            )
        elif reference_images:
            logger(
                "Reference images were selected, but the current Imagen model call does not "
                "support attached image parts. They will be ignored for this run."
            )

        gemini_reference_images = (
            reference_images
            if image_model_config["family"] == "gemini_image"
            or is_gemini_image_model(image_model_config["id"])
            else []
        )
        image_worker_count = min(max_image_workers, max(1, len(scenes)))
        image_paths: list[Path | None] = [None] * len(scenes)
        image_errors: list[Exception | None] = [None] * len(scenes)

        def generate_scene_image(index: int, scene: Scene) -> tuple[int, Path]:
            image_path = images_dir / f"scene_{scene.sceneIndex:03d}.png"
            worker_client = GeminiVideoClient(
                api_key=api_key,
                logger=logger,
                token_usage=token_usage,
            )
            prompt_parts = [scene.imagePrompt]
            if shared_prompt:
                prompt_parts.append(f"Shared visual instructions: {shared_prompt}")
            if job.video_prompt:
                prompt_parts.append(
                    f"Video-specific visual instructions: {job.video_prompt}"
                )
            saved_path = worker_client.generate_image(
                "\n\n".join(prompt_parts),
                image_model_config,
                image_path,
                reference_images=gemini_reference_images,
            )
            return index, saved_path

        logger(
            f"Generating {len(scenes)} images in parallel "
            f"with {image_worker_count} worker(s)."
        )
        with ThreadPoolExecutor(max_workers=image_worker_count) as executor:
            future_to_index = {
                executor.submit(generate_scene_image, index, scene): index
                for index, scene in enumerate(scenes)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                scene = scenes[index]
                try:
                    _, saved_path = future.result()
                    image_paths[index] = saved_path
                    logger(f"Saved image for scene {scene.sceneIndex}: {saved_path}")
                except Exception as image_exc:
                    image_errors[index] = image_exc
                    logger(
                        f"Image generation failed for scene {scene.sceneIndex}: {image_exc}"
                    )

        final_token_usage = save_token_usage()
        logger(f"Saved token usage: {token_usage_path}")
        logger(format_token_usage(final_token_usage))

        if not any(image_paths):
            first_error = next((error for error in image_errors if error), None)
            raise RuntimeError(
                f"All image generation requests failed. First error: {first_error}"
            ) from first_error

        for index, scene in enumerate(scenes):
            if image_paths[index] is not None:
                continue

            source_path: Path | None = None
            for distance in range(1, len(image_paths)):
                previous_index = index - distance
                next_index = index + distance
                if previous_index >= 0 and image_paths[previous_index] is not None:
                    source_path = image_paths[previous_index]
                    break
                if next_index < len(image_paths) and image_paths[next_index] is not None:
                    source_path = image_paths[next_index]
                    break

            if source_path is None:
                raise RuntimeError(f"No fallback image available for scene {scene.sceneIndex}.")

            image_path = images_dir / f"scene_{scene.sceneIndex:03d}.png"
            shutil.copyfile(source_path, image_path)
            image_paths[index] = image_path
            logger(
                f"Reused nearest successful image for scene {scene.sceneIndex}: {image_path}"
            )

        resolved_image_paths: list[Path] = []
        for scene, image_path in zip(scenes, image_paths):
            if image_path is None:
                raise RuntimeError(f"Missing image for scene {scene.sceneIndex}.")
            resolved_image_paths.append(image_path)

        write_srt(scenes, srt_path)
        logger(f"Saved SRT: {srt_path}")

        tts_client = TtsForFreeClient(
            endpoint=endpoint,
            logger=logger,
            api_key=tts_api_key,
        )
        tts_job_id = tts_client.submit_srt_job(srt_path, job.voice)
        audio_url = tts_client.poll_until_completed(tts_job_id)
        final_audio_path = tts_client.download_audio(audio_url, audio_path)
        logger(f"Downloaded audio: {final_audio_path}")

        assembler = VideoAssembler(logger=logger)
        assembler.create_image_video(
            scenes=scenes,
            image_paths=resolved_image_paths,
            manifest_path=concat_manifest_path,
            output_video_path=image_video_path,
        )
        logger(f"Created image video: {image_video_path}")

        assembler.merge_video_audio_and_subtitles(
            image_video_path=image_video_path,
            audio_path=final_audio_path,
            srt_path=srt_path,
            final_output_path=final_output_path,
        )
        logger(f"Final video saved: {final_output_path}")

        append_generation_history(
            {
                "run_id": run_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "batch_position": job.position,
                "video_mode": job.video_mode,
                "idea": job.idea,
                "shared_prompt": shared_prompt,
                "video_prompt": job.video_prompt,
                "voice": job.voice,
                "image_model_label": image_model_label,
                "parallel_image_requests": max_image_workers,
                "reference_source": reference_source,
                "reference_image_paths": list(job.reference_image_paths),
                "input_tokens": final_token_usage["input_tokens"],
                "output_tokens": final_token_usage["output_tokens"],
                "token_api_requests": final_token_usage["api_requests"],
                "token_requests_missing_counts": final_token_usage[
                    "requests_missing_token_counts"
                ],
                "token_usage": final_token_usage,
                "token_usage_path": str(token_usage_path),
                "final_video_path": str(final_output_path),
                "output_dir": str(output_dir),
                "temp_dir": str(temp_dir),
                "scenes_json_path": str(scenes_json_path),
                "srt_path": str(srt_path),
                "audio_path": str(final_audio_path),
                "script_text": build_plain_script_from_scenes(scenes),
                "srt_text": read_text_if_exists(srt_path),
            }
        )
        logger(f"Saved generation history: {HISTORY_PATH.name}")

        elapsed = time.time() - started_at
        logger(f"Done in {elapsed:.1f}s")
        return final_output_path


def main() -> None:
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    app = GeminiTtsForFreeApp(root)
    app.log("Tool ready.")
    root.mainloop()


if __name__ == "__main__":
    main()
