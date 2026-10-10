from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .media_utils import ensure_ffmpeg, probe_media
from .settings import SettingsStore
from .silence_trim import (
    DEFAULT_FPS,
    DEFAULT_KEEP_GAP_MS,
    DEFAULT_MIN_SILENCE_MS,
    DEFAULT_NOISE_DB,
    TrimOptions,
    TrimPlan,
    analyze,
    render_trimmed,
)
from .srt_utils import format_timestamp


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / "silence_trimmer.settings.json"
AUDIO_TYPES = (
    ("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
    ("All files", "*.*"),
)
VIDEO_TYPES = (
    ("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"),
    ("All files", "*.*"),
)
OUTPUT_SUFFIX = "_trimmed"


def short_time(seconds: float) -> str:
    text = format_timestamp(seconds)
    return text[3:] if text.startswith("00:") else text


class SilenceTrimWindow:
    """Shorten long pauses in the voice MP3 and cut the MP4 at the same places."""

    def __init__(self, window: tk.Tk | tk.Toplevel):
        self.window = window
        self.window.title("Cắt khoảng lặng MP3 + cắt MP4 theo")
        self.window.geometry("980x700")
        self.window.minsize(820, 560)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closed = False
        self.last_output: Path | None = None
        self.settings = SettingsStore(SETTINGS_PATH)

        self.audio_var = tk.StringVar()
        self.video_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.noise_var = tk.StringVar(value=f"{DEFAULT_NOISE_DB:g}")
        self.min_silence_var = tk.StringVar(value=str(DEFAULT_MIN_SILENCE_MS))
        self.keep_gap_var = tk.StringVar(value=str(DEFAULT_KEEP_GAP_MS))
        self.trim_edges_var = tk.BooleanVar(value=False)
        self.export_mp3_var = tk.BooleanVar(value=True)
        self.summary_var = tk.StringVar(value="Chưa phân tích.")
        self.status_var = tk.StringVar(
            value="Chọn MP3 giọng đọc và MP4 (timeline theo MP3), bấm Phân tích để xem chỗ cắt rồi Cắt + xuất."
        )

        self._build_ui()
        self._load_settings()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.after(120, self._flush_events)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        main = ttk.Frame(self.window, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        files = ttk.LabelFrame(main, text="File", padding=6)
        files.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        files.columnconfigure(1, weight=1)
        for row, (label, var, command) in enumerate(
            (
                ("MP3 giọng đọc", self.audio_var, self._choose_audio),
                ("MP4 (có thể không tiếng)", self.video_var, self._choose_video),
                ("Thư mục xuất", self.output_dir_var, self._choose_output_dir),
            )
        ):
            ttk.Label(files, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(files, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Button(files, text="Chọn...", command=command).grid(row=row, column=2, padx=(8, 0), pady=2)
        ttk.Label(
            files,
            text="Để trống MP4 = chỉ cắt MP3. Để trống thư mục xuất = lưu cạnh file gốc, tên *_trimmed.",
        ).grid(row=3, column=1, columnspan=2, sticky="w")

        opts = ttk.LabelFrame(main, text="Khoảng lặng", padding=6)
        opts.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(opts, text="Giữ lại giữa 2 câu (ms)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, textvariable=self.keep_gap_var, from_=0, to=2000, increment=50, width=6).grid(
            row=0, column=1, padx=(4, 16)
        )
        ttk.Label(opts, text="Chỉ cắt khi lặng dài hơn (ms)").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(opts, textvariable=self.min_silence_var, from_=100, to=10000, increment=50, width=6).grid(
            row=0, column=3, padx=(4, 16)
        )
        ttk.Label(opts, text="Ngưỡng im lặng (dB)").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(opts, textvariable=self.noise_var, from_=-80, to=-10, increment=5, width=5).grid(
            row=0, column=5, padx=(4, 0)
        )
        ttk.Checkbutton(
            opts, text="Cắt cả khoảng lặng đầu/cuối file", variable=self.trim_edges_var
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opts, text="Xuất thêm MP3 đã cắt", variable=self.export_mp3_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            opts,
            text="(ngưỡng: tiếng ồn nền bị coi là nói thì đổi -35/-30; bị lẹm cuối chữ thì đổi -45/-50)",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.analyze_button = ttk.Button(actions, text="Phân tích khoảng lặng", command=lambda: self._start(False))
        self.analyze_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="Cắt + xuất", command=lambda: self._start(True))
        self.run_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(actions, text="Dừng", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        self.open_button = ttk.Button(actions, text="Mở file đã xuất", command=self._open_output, state="disabled")
        self.open_button.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(actions, mode="determinate", maximum=100, length=220)
        self.progress.pack(side="right")

        table = ttk.LabelFrame(main, text="Các đoạn sẽ cắt (theo thời gian của MP3 gốc)", padding=6)
        table.grid(row=3, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        ttk.Label(table, textvariable=self.summary_var).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.tree = ttk.Treeview(table, columns=("index", "silence", "cut", "removed"), show="headings")
        for column, title, width in (
            ("index", "#", 50),
            ("silence", "Khoảng lặng gốc", 260),
            ("cut", "Đoạn bị cắt", 260),
            ("removed", "Bỏ", 100),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column in {"silence", "cut"})
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        ttk.Label(main, textvariable=self.status_var).grid(row=4, column=0, sticky="w", pady=(6, 0))

    # ---------- settings ----------
    def _load_settings(self) -> None:
        data = self.settings.load()
        self.output_dir_var.set(str(data.get("output_dir", "")))
        self.noise_var.set(str(data.get("noise_db", self.noise_var.get())))
        self.min_silence_var.set(str(data.get("min_silence_ms", self.min_silence_var.get())))
        self.keep_gap_var.set(str(data.get("keep_gap_ms", self.keep_gap_var.get())))
        self.trim_edges_var.set(bool(data.get("trim_edges", False)))
        self.export_mp3_var.set(bool(data.get("export_mp3", True)))

    def _save_settings(self) -> None:
        self.settings.save(
            {
                "output_dir": self.output_dir_var.get().strip(),
                "noise_db": self.noise_var.get().strip(),
                "min_silence_ms": self.min_silence_var.get().strip(),
                "keep_gap_ms": self.keep_gap_var.get().strip(),
                "trim_edges": self.trim_edges_var.get(),
                "export_mp3": self.export_mp3_var.get(),
            }
        )

    # ---------- file pickers ----------
    def _choose_audio(self) -> None:
        path = filedialog.askopenfilename(parent=self.window, title="Chọn MP3 giọng đọc", filetypes=AUDIO_TYPES)
        if not path:
            return
        self.audio_var.set(path)
        if not self.video_var.get().strip():
            # Same-name video next to the MP3 (S08_en.mp3 -> S08_en.mp4).
            for suffix in (".mp4", ".mov", ".mkv"):
                candidate = Path(path).with_suffix(suffix)
                if candidate.is_file():
                    self.video_var.set(str(candidate))
                    break

    def _choose_video(self) -> None:
        path = filedialog.askopenfilename(parent=self.window, title="Chọn MP4", filetypes=VIDEO_TYPES)
        if path:
            self.video_var.set(path)

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.window, title="Thư mục lưu file đã cắt")
        if path:
            self.output_dir_var.set(path)

    def _open_output(self) -> None:
        if not self.last_output or not self.last_output.is_file():
            return
        try:
            os.startfile(str(self.last_output))  # type: ignore[attr-defined]
        except AttributeError:
            self.status_var.set(f"File đã xuất: {self.last_output}")
        except OSError as exc:
            messagebox.showerror("Không mở được", str(exc), parent=self.window)

    # ---------- running ----------
    def _read_config(self) -> dict[str, Any]:
        audio = Path(self.audio_var.get().strip().strip('"'))
        if not audio.is_file():
            raise ValueError("Chọn file MP3 giọng đọc.")
        raw_video = self.video_var.get().strip().strip('"')
        video = Path(raw_video) if raw_video else None
        if video is not None and not video.is_file():
            raise ValueError(f"Không tìm thấy video:\n{video}")
        try:
            noise_db = float(self.noise_var.get())
            min_silence = float(self.min_silence_var.get()) / 1000
            keep_gap = float(self.keep_gap_var.get()) / 1000
        except ValueError:
            raise ValueError("Ngưỡng dB và thời gian (ms) phải là số.") from None
        if keep_gap < 0 or min_silence <= 0:
            raise ValueError("Thời gian phải lớn hơn 0.")
        if min_silence <= keep_gap:
            raise ValueError("'Chỉ cắt khi lặng dài hơn' phải lớn hơn 'Giữ lại giữa 2 câu'.")
        return {
            "audio": audio,
            "video": video,
            "output_dir": self.output_dir_var.get().strip(),
            "export_mp3": self.export_mp3_var.get(),
            "options": TrimOptions(
                noise_db=min(-1.0, noise_db),
                min_silence=min_silence,
                keep_gap=keep_gap,
                trim_edges=self.trim_edges_var.get(),
            ),
        }

    def _start(self, render: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._read_config()
            ensure_ffmpeg()
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Chưa chạy được", str(exc), parent=self.window)
            return
        self.cancel_event.clear()
        self.progress.configure(value=0)
        self._set_busy(True)
        self.worker = threading.Thread(target=self._run, args=(config, render), daemon=True)
        self.worker.start()

    def _run(self, config: dict[str, Any], render: bool) -> None:
        try:
            audio: Path = config["audio"]
            video: Path | None = config["video"]
            self.events.put(("status", "Đang đọc file..."))
            audio_info = probe_media(audio)
            if not audio_info.has_audio or audio_info.duration <= 0:
                raise RuntimeError("File MP3 không có âm thanh.")
            video_duration, fps = 0.0, DEFAULT_FPS
            if video is not None:
                video_info = probe_media(video)
                if video_info.duration <= 0 or not video_info.width:
                    raise RuntimeError("Không đọc được video.")
                video_duration, fps = video_info.duration, video_info.fps or DEFAULT_FPS
            self.events.put(("status", "Đang tìm khoảng lặng..."))
            plan = analyze(audio, config["options"], audio_info.duration, video_duration, fps)
            self.events.put(("plan", plan))
            if not render:
                self.events.put(("done", "Đã phân tích. Bấm Cắt + xuất để tạo file."))
                return
            if not plan.cuts:
                self.events.put(("done", "Không có khoảng lặng nào cần cắt."))
                return

            out_dir = Path(config["output_dir"]) if config["output_dir"] else (video or audio).parent
            outputs: list[Path] = []
            jobs: list[tuple[Path, Path | None]] = []
            if video is not None:
                jobs.append((out_dir / f"{video.stem}{OUTPUT_SUFFIX}.mp4", video))
            if video is None or config["export_mp3"]:
                jobs.append((out_dir / f"{audio.stem}{OUTPUT_SUFFIX}.mp3", None))
            for index, (output, source_video) in enumerate(jobs):
                label = "video" if source_video else "MP3"
                self.events.put(("status", f"Đang cắt {label} ({index + 1}/{len(jobs)})..."))

                def on_progress(value: float, index: int = index) -> None:
                    self.events.put(("progress", (index + value) / len(jobs) * 100))

                outputs.append(
                    render_trimmed(
                        plan,
                        audio,
                        output,
                        video=source_video,
                        video_duration=video_duration,
                        fps=fps,
                        on_progress=on_progress,
                        cancel_event=self.cancel_event,
                    )
                )
            self.events.put(("output", outputs[0]))
            self.events.put(("done", "Xong: " + " | ".join(str(path) for path in outputs)))
        except InterruptedError:
            self.events.put(("done", "Đã dừng."))
        except Exception as exc:
            traceback.print_exc()
            self.events.put(("error", str(exc)))

    def _show_plan(self, plan: TrimPlan) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, ((start, end), (cut_start, cut_end)) in enumerate(
            zip(plan.cut_sources, plan.cuts), start=1
        ):
            self.tree.insert(
                "",
                "end",
                values=(
                    index,
                    f"{short_time(start)} → {short_time(end)}  ({end - start:.2f}s)",
                    f"{short_time(cut_start)} → {short_time(cut_end)}",
                    f"{cut_end - cut_start:.2f}s",
                ),
            )
        self.summary_var.set(
            f"{len(plan.cuts)} chỗ cắt, bỏ {plan.removed:.1f}s: "
            f"{short_time(plan.duration)} → {short_time(plan.new_duration)}"
        )

    def _flush_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self.progress.configure(value=float(payload))
                elif kind == "plan":
                    self._show_plan(payload)
                elif kind == "output":
                    self.last_output = payload
                elif kind == "done":
                    self._set_busy(False)
                    if str(payload).startswith("Xong"):
                        self.progress.configure(value=100)
                    self.status_var.set(str(payload))
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set("Lỗi.")
                    messagebox.showerror("Lỗi", str(payload), parent=self.window)
        except queue.Empty:
            pass
        self.window.after(120, self._flush_events)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.analyze_button.configure(state=state)
        self.run_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")
        has_output = not busy and self.last_output is not None and self.last_output.is_file()
        self.open_button.configure(state="normal" if has_output else "disabled")

    def _stop(self) -> None:
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("Đang dừng...")

    def _on_close(self) -> None:
        self.cancel_event.set()
        try:
            self._save_settings()
        except Exception:
            pass
        self.closed = True
        self.window.destroy()


def main() -> None:
    root = tk.Tk()
    SilenceTrimWindow(root)
    root.mainloop()
