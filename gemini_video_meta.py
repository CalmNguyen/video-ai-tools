"""
Gemini batch tool: videos -> SRT (Gemini listens to the audio) -> title,
description and thumbnail prompt.

Install:
    pip install -U google-genai
"""

from gemini_full_video_editor.video_meta_app import main


if __name__ == "__main__":
    main()
