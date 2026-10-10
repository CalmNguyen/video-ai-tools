"""
Gemini SRT Fixer launcher (Python 3.11+).

Fixes misrecognized words in an SRT using a reference text / context, keeping
every timestamp unchanged.

Install:
    pip install -U google-genai
"""

from gemini_full_video_editor.srt_fixer_app import main


if __name__ == "__main__":
    main()
