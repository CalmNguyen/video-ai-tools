from __future__ import annotations

import ast
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _load_stdlib_copy_before_google_modules() -> None:
    """Prevent this repository's copy.py from shadowing Python's stdlib copy."""
    existing = sys.modules.get("copy")
    existing_file = getattr(existing, "__file__", "") if existing else ""
    repository_copy = Path(__file__).resolve().parents[1] / "copy.py"
    if existing and (not existing_file or Path(existing_file).resolve() != repository_copy):
        return
    if existing:
        sys.modules.pop("copy", None)
    original_path = list(sys.path)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        sys.path[:] = [
            entry
            for entry in original_path
            if Path(entry or os.getcwd()).resolve() != repository_root
        ]
        __import__("copy")
    finally:
        sys.path[:] = original_path


_load_stdlib_copy_before_google_modules()

try:
    from google import genai
    from google.genai import types
except ImportError:  # Reported with a friendly error when a client is created.
    genai = None
    types = None


RETRY_DELAYS = (5, 15, 30)


def ensure_google_genai() -> None:
    if genai is None or types is None:
        raise RuntimeError("Missing google-genai. Run: pip install -U google-genai")


def extract_json(raw_text: str) -> Any:
    text = (raw_text or "").strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char not in "[{":
            continue
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
    raise RuntimeError("Gemini returned malformed JSON.")


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
    if not parts:
        raise RuntimeError("Gemini did not return text content.")
    return "\n".join(parts)


def retryable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "rate limit",
            "quota",
            "resource exhausted",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "temporarily unavailable",
            "connection",
        )
    )


class GeminiJsonClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        logger: Callable[[str], None],
        cancel_event: threading.Event,
    ):
        ensure_google_genai()
        self.client = genai.Client(api_key=api_key.strip())
        self.model = model.strip()
        self.logger = logger
        self.cancel_event = cancel_event

    def generate_json(
        self,
        prompt: str | list[Any],
        label: str,
        max_output_tokens: int = 32768,
    ) -> Any:
        """prompt is either plain text or a contents list (e.g. [audio_part, text])."""
        last_error: Exception | None = None
        for attempt in range(1, len(RETRY_DELAYS) + 2):
            if self.cancel_event.is_set():
                raise InterruptedError("Stopped by user.")
            try:
                self.logger(f"Gemini [{label}] request {attempt}...")
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        max_output_tokens=max_output_tokens,
                    ),
                )
                return extract_json(response_text(response))
            except InterruptedError:
                raise
            except Exception as exc:
                last_error = exc
                can_retry = attempt <= len(RETRY_DELAYS)
                should_retry = retryable_error(exc) or "json" in str(exc).lower()
                if not can_retry or not should_retry:
                    raise
                delay = RETRY_DELAYS[attempt - 1]
                self.logger(f"Gemini error: {exc}. Retrying in {delay} seconds...")
                if self.cancel_event.wait(delay):
                    raise InterruptedError("Stopped by user.")
        raise RuntimeError(str(last_error) if last_error else "Gemini request failed.")

    @staticmethod
    def audio_part(path: Path, mime_type: str = "audio/mpeg") -> Any:
        ensure_google_genai()
        return types.Part.from_bytes(data=Path(path).read_bytes(), mime_type=mime_type)

    def summarize_script(self, script: str) -> str:
        prompt = f"""
Summarize the following original video script for use as context by a semantic
subtitle aligner and video editing planner. Preserve the topic, narrative order,
section boundaries, tutorial actions, product/UI names, numbers, hook, transitions,
and CTA. Do not translate quoted UI labels. Return JSON only:
{{"summary":"...","sections":[{{"title":"...","content":"..."}}]}}

ORIGINAL SCRIPT:
{script}
""".strip()
        payload = self.generate_json(prompt, "script context", max_output_tokens=8192)
        if not isinstance(payload, dict):
            raise RuntimeError("Gemini script summary is not a JSON object.")
        summary = str(payload.get("summary", "")).strip()
        sections = payload.get("sections", [])
        section_text = "\n".join(
            f"- {item.get('title', '')}: {item.get('content', '')}"
            for item in sections
            if isinstance(item, dict)
        )
        context = (summary + "\n\nSECTIONS:\n" + section_text).strip()
        if not context:
            raise RuntimeError("Gemini returned an empty script summary.")
        return context
