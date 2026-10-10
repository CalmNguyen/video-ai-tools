"""
Shorten long pauses in a voice MP3 and cut the MP4 (timeline follows the MP3) at the same places.

Install:
    FFmpeg and ffprobe must be in PATH (no API key needed).
"""

from gemini_full_video_editor.silence_trim_app import main


if __name__ == "__main__":
    main()
