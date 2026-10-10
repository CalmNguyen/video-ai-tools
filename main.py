"""
VideoAI launcher: one entry point that opens any of the tools in this folder.

Run:
    python main.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk


ROOT = Path(__file__).resolve().parent

TOOLS = (
    (
        "Cắt Short từ video dài",
        "Gemini đọc SRT của video dài, chọn các đoạn hay và xuất nhiều video short.",
        ROOT / "gemini_video_short_cutter.py",
    ),
    (
        "Full Video Editor (giọng mới)",
        "Dựng lại video gốc theo timeline giọng đọc mới, Gemini lên edit plan.",
        ROOT / "gemini_ai_full_video_editor.py",
    ),
    (
        "Tự lên kịch bản + TTS + dựng video",
        "Từ ý tưởng: Gemini viết kịch bản, tạo ảnh, TTS và dựng video hoàn chỉnh.",
        ROOT / "gemini_ttsforfree_video_tool.py",
    ),
    (
        "Sửa SRT bằng Gemini",
        "Dán SRT bị nhận sai chữ + văn bản mẫu/ngữ cảnh; Gemini sửa chữ, giữ nguyên timestamp.",
        ROOT / "gemini_srt_fixer.py",
    ),
    (
        "Video → SRT + Title/Desc/Thumbnail",
        "Tải nhiều video, Gemini lấy SRT rồi viết title, description, prompt thumbnail (chọn model riêng).",
        ROOT / "gemini_video_meta.py",
    ),
    (
        "Tải video (Bilibili, YouTube...)",
        "Tải video/audio bằng yt-dlp. Cần: pip install PySide6 yt-dlp.",
        ROOT / "download_bilibili" / "main.py",
    ),
)


class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VideoAI Tools")
        self.root.resizable(False, False)
        self.status_var = tk.StringVar(value="Chọn một tool để mở.")

        main = ttk.Frame(root, padding=16)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="VideoAI", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            main, text="Mỗi tool mở trong một cửa sổ riêng; có thể mở nhiều tool cùng lúc."
        ).pack(anchor="w", pady=(0, 12))

        for title, description, script in TOOLS:
            card = ttk.Frame(main, padding=(0, 6))
            card.pack(fill="x")
            ttk.Button(
                card,
                text=title,
                width=36,
                command=lambda path=script, name=title: self._launch(path, name),
            ).pack(side="left")
            ttk.Label(card, text=description, wraplength=380, justify="left").pack(
                side="left", padx=(12, 0)
            )

        ttk.Separator(main).pack(fill="x", pady=(12, 8))
        ttk.Label(main, textvariable=self.status_var).pack(anchor="w")

    def _launch(self, script: Path, title: str) -> None:
        if not script.is_file():
            messagebox.showerror("Không tìm thấy tool", f"Thiếu file:\n{script}")
            return
        try:
            subprocess.Popen([sys.executable, str(script)], cwd=str(script.parent))
        except OSError as exc:
            messagebox.showerror("Không mở được tool", str(exc))
            return
        self.status_var.set(f"Đã mở: {title}")


def main() -> None:
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
