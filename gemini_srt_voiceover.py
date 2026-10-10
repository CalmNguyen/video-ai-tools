"""
SRT -> TTS For Free voice -> mixed into the original video (batch).

Install:
    pip install -U requests cryptography
    (FFmpeg and ffprobe must be in PATH)
"""

from gemini_full_video_editor.voiceover_app import main


if __name__ == "__main__":
    main()
