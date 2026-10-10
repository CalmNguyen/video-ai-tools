from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .gemini_client import GeminiJsonClient
from .models import Cue


LANGUAGE_AUTO = "Giống ngôn ngữ trong video"
LANGUAGES = (LANGUAGE_AUTO, "Tiếng Việt", "English")
MAX_TRANSCRIPT_CHARS = 200_000


@dataclass
class VideoMetadata:
    title: str
    description: str
    thumbnail_prompt: str
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "thumbnail_prompt": self.thumbnail_prompt,
            "tags": self.tags,
        }

    def to_text(self) -> str:
        tags = ", ".join(self.tags)
        return (
            f"TITLE:\n{self.title}\n\nDESCRIPTION:\n{self.description}\n\n"
            f"THUMBNAIL PROMPT:\n{self.thumbnail_prompt}\n\nTAGS:\n{tags}\n"
        )


def generate_video_metadata(
    client: GeminiJsonClient,
    video_name: str,
    cues: Sequence[Cue],
    language: str = LANGUAGE_AUTO,
    extra_instructions: str = "",
) -> VideoMetadata:
    """Ask Gemini for a title, description and thumbnail image prompt from the transcript."""
    transcript = "\n".join(cue.text for cue in cues)[:MAX_TRANSCRIPT_CHARS]
    if not transcript.strip():
        raise RuntimeError(f"{video_name} has an empty transcript.")
    if language == LANGUAGE_AUTO:
        language_rule = "Write the title and description in the same language as the transcript."
    else:
        language_rule = f"Write the title and description in {language}."
    extra = (
        f"\nEXTRA INSTRUCTIONS FROM THE CHANNEL OWNER:\n{extra_instructions.strip()}\n"
        if extra_instructions.strip()
        else ""
    )
    prompt = f"""
You are a YouTube growth expert. From the transcript of the video "{video_name}",
write publishing metadata that maximizes click-through rate without being
misleading: it must honestly reflect what the video actually covers.

- {language_rule}
- title: one strong, curiosity-driven title, at most 70 characters, key
  keyword near the start, no clickbait lies, no emoji spam.
- description: a hook in the first two lines, then a short summary of the
  value of the video, key points as a bullet list, a call to action, and
  3-5 relevant hashtags at the end. Use only facts present in the transcript.
- thumbnail_prompt: ONE detailed prompt in ENGLISH for an AI image generator
  (16:9, 1280x720). Describe the subject, expression/emotion, composition,
  background, lighting, color palette (high contrast), and at most 3-5 words
  of bold overlay text (quote the exact text, in the video's language). No
  logos or real people's names.
- tags: 8-15 search keywords.
{extra}
Return JSON only:
{{"title":"...","description":"...","thumbnail_prompt":"...","tags":["..."]}}

TRANSCRIPT:
{transcript}
""".strip()
    payload = client.generate_json(prompt, f"metadata {video_name}", max_output_tokens=8192)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError("Gemini metadata is not a JSON object.")
    title = " ".join(str(payload.get("title", "")).split())
    description = str(payload.get("description", "")).strip()
    thumbnail = str(payload.get("thumbnail_prompt", "")).strip()
    if not (title and description and thumbnail):
        raise RuntimeError("Gemini returned incomplete metadata.")
    raw_tags = payload.get("tags")
    tags = (
        [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        if isinstance(raw_tags, list)
        else []
    )
    return VideoMetadata(title, description, thumbnail, tags)
