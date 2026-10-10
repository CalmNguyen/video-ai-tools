from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .media_utils import ensure_ffmpeg, probe_media
from .settings import SettingsStore
from .silence_trim import (
    DEFAULT_FPS,
    DEFAULT_KEEP_GAP_MS,
    DEFAULT_MIN_SILENCE_MS,
    DEFAULT_NOISE_DB,
    DEFAULT_SHORT_LIMIT,
    LAYOUT_LABELS,
    LAYOUT_ORIGINAL,
    LAYOUT_SHORT,
    VERTICAL_SUFFIX,
    MusicOptions,
    TrimOptions,
    analyze,
    no_cut_plan,
    render_trimmed,
    resolve_layout,
)
from .srt_utils import format_timestamp, parse_srt, read_text_file
from .ttsforfree_srt import (
    DEFAULT_BLOCK_SPEED,
    DEFAULT_ENDPOINT,
    DEFAULT_MAX_SPEECH_RATE,
    DEFAULT_MIN_SPEECH_RATE,
    MAX_SPEECH_RATE,
    MIN_SPEECH_RATE,
    TtsForFreeSrtClient,
    clamp_speech_rates,
)
from .voiceover import MixOptions, mix_voiceover, srt_fingerprint


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / "gemini_srt_voiceover.settings.json"
TTS_TOOL_SETTINGS_PATH = PACKAGE_ROOT / "gemini_ttsforfree_video_tool.settings.json"
VIDEO_TYPES = (
    ("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv"),
    ("All files", "*.*"),
)
WORK_DIR_NAME = "voiceover_work"
OUTPUT_SUFFIX = "_voiced"
MAX_PARALLEL_VIDEOS = 4
MUSIC_TYPES = (
    ("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
    ("All files", "*.*"),
)
# Where pauses are looked for before shortening them.
DETECT_MIX = "mix"
DETECT_VOICE = "voice"
DETECT_LABELS = {
    DETECT_MIX: "Âm thanh sau khi ghép (giọng mới + âm gốc)",
    DETECT_VOICE: "Chỉ giọng mới (TTS)",
}


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


@dataclass
class VideoItem:
    path: Path
    srt_text: str = ""
    layout: str = LAYOUT_ORIGINAL
    status: str = "-"
    output: Path | None = None


def cue_summary(srt_text: str) -> str:
    if not srt_text.strip():
        return "Chưa có SRT"
    try:
        cues = parse_srt(srt_text)
    except ValueError:
        return "SRT lỗi"
    if not cues:
        return "SRT lỗi"
    return f"{len(cues)} cue, đến {format_timestamp(cues[-1].end, True)[:8]}"


class VoiceoverWindow:
    """Batch: paste/load an SRT per video, voice it with TTS For Free, mix into the video."""

    def __init__(self, window: tk.Tk | tk.Toplevel):
        self.window = window
        self.window.title("SRT → Giọng TTS For Free → Ghép vào video")
        self.window.geometry("1180x940")
        self.window.minsize(920, 760)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closed = False
        self.items: dict[str, VideoItem] = {}
        self.current_iid: str | None = None
        self.settings = SettingsStore(SETTINGS_PATH)

        self.endpoint_var = tk.StringVar(value=DEFAULT_ENDPOINT)
        self.api_key_var = tk.StringVar()
        self.remember_key_var = tk.BooleanVar(value=False)
        self.voice_var = tk.StringVar()
        self.min_rate_var = tk.StringVar(value=f"{DEFAULT_MIN_SPEECH_RATE:.2f}")
        self.max_rate_var = tk.StringVar(value=f"{DEFAULT_MAX_SPEECH_RATE:.2f}")
        self.orig_volume_var = tk.StringVar(value="100")
        self.voice_volume_var = tk.StringVar(value="100")
        self.duck_var = tk.BooleanVar(value=True)
        self.reuse_var = tk.BooleanVar(value=True)
        self.parallel_var = tk.StringVar(value="2")
        self.output_dir_var = tk.StringVar()
        # Finishing after the voice is mixed in: shorten pauses, reframe, background music.
        self.trim_var = tk.BooleanVar(value=True)
        self.keep_gap_var = tk.StringVar(value=str(DEFAULT_KEEP_GAP_MS))
        self.min_silence_var = tk.StringVar(value=str(DEFAULT_MIN_SILENCE_MS))
        self.noise_var = tk.StringVar(value=f"{DEFAULT_NOISE_DB:g}")
        self.detect_var = tk.StringVar(value=DETECT_LABELS[DETECT_MIX])
        self.trim_edges_var = tk.BooleanVar(value=False)
        self.layout_var = tk.StringVar(value=LAYOUT_LABELS[LAYOUT_ORIGINAL])
        self.short_limit_var = tk.StringVar(value=str(DEFAULT_SHORT_LIMIT))
        self.music_var = tk.BooleanVar(value=False)
        self.music_path_var = tk.StringVar()
        self.music_volume_var = tk.StringVar(value="15")
        self.music_duck_var = tk.BooleanVar(value=True)
        self.music_fade_in_var = tk.StringVar(value="1")
        self.music_fade_out_var = tk.StringVar(value="2")
        self.srt_info_var = tk.StringVar(value="Chọn một video để dán SRT.")
        self.status_var = tk.StringVar(
            value="Thêm video, chọn từng video rồi dán SRT (hoặc mở file .srt), sau đó bấm Tạo giọng + ghép."
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
        main.rowconfigure(5, weight=2)

        api = ttk.LabelFrame(main, text="TTS For Free", padding=6)
        api.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        api.columnconfigure(1, weight=1)
        api.columnconfigure(3, weight=1)
        ttk.Label(api, text="API key").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(api, textvariable=self.api_key_var, show="*").grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(api, text="Nhớ API key", variable=self.remember_key_var).grid(
            row=0, column=2, padx=(8, 16)
        )
        ttk.Label(api, text="Endpoint").grid(row=0, column=3, sticky="e", padx=(0, 8))
        ttk.Entry(api, textvariable=self.endpoint_var, width=34).grid(row=0, column=4, sticky="ew")
        ttk.Label(api, text="Giọng (voice)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(api, textvariable=self.voice_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=(6, 0)
        )
        rates = ttk.Frame(api)
        rates.grid(row=1, column=3, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(rates, text="Tốc độ đọc tự khớp từ").pack(side="left")
        spin = dict(from_=MIN_SPEECH_RATE, to=MAX_SPEECH_RATE, increment=0.05, width=5, format="%.2f")
        ttk.Spinbox(rates, textvariable=self.min_rate_var, **spin).pack(side="left", padx=4)
        ttk.Label(rates, text="đến").pack(side="left")
        ttk.Spinbox(rates, textvariable=self.max_rate_var, **spin).pack(side="left", padx=4)
        ttk.Label(rates, text="(bằng nhau = tốc độ cố định)").pack(side="left", padx=(4, 0))

        mix = ttk.LabelFrame(main, text="Ghép âm thanh & xuất", padding=6)
        mix.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        mix.columnconfigure(8, weight=1)
        ttk.Label(mix, text="Âm gốc %").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(mix, textvariable=self.orig_volume_var, from_=0, to=300, increment=10, width=5).grid(
            row=0, column=1, padx=(4, 12)
        )
        ttk.Label(mix, text="Giọng mới %").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(mix, textvariable=self.voice_volume_var, from_=0, to=300, increment=10, width=5).grid(
            row=0, column=3, padx=(4, 12)
        )
        ttk.Checkbutton(mix, text="Hạ âm gốc khi giọng mới nói", variable=self.duck_var).grid(
            row=0, column=4, padx=(0, 12)
        )
        ttk.Checkbutton(
            mix, text="Dùng lại giọng đã tạo nếu SRT không đổi (không tốn credit)", variable=self.reuse_var
        ).grid(row=0, column=5, columnspan=3, sticky="w")
        ttk.Label(mix, text="Thư mục xuất").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(mix, textvariable=self.output_dir_var).grid(
            row=1, column=1, columnspan=7, sticky="ew", pady=(6, 0), padx=(4, 0)
        )
        ttk.Button(mix, text="Chọn...", command=self._choose_output_dir).grid(
            row=1, column=8, sticky="w", padx=(8, 0), pady=(6, 0)
        )
        ttk.Label(mix, text="Số video chạy song song").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Spinbox(mix, textvariable=self.parallel_var, from_=1, to=MAX_PARALLEL_VIDEOS, width=4).grid(
            row=2, column=2, sticky="w", padx=(4, 0), pady=(6, 0)
        )
        ttk.Label(mix, text="(để trống thư mục xuất = lưu cạnh từng video, tên *_voiced.mp4)").grid(
            row=2, column=3, columnspan=6, sticky="w", padx=(12, 0), pady=(6, 0)
        )

        list_frame = ttk.Frame(main)
        self._build_finish(main)

        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            list_frame,
            columns=("video", "srt", "layout", "status"),
            show="headings",
            selectmode="extended",
            height=6,
        )
        for column, title, width in (
            ("video", "Video", 400),
            ("srt", "SRT", 180),
            ("layout", "Khung", 130),
            ("status", "Trạng thái", 340),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column in {"video", "status"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._open_output)

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=6)
        ttk.Button(actions, text="Thêm video...", command=self._add_videos).pack(side="left")
        ttk.Button(actions, text="Xóa đã chọn", command=self._remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Xóa hết", command=self._clear).pack(side="left", padx=(6, 0))
        ttk.Separator(actions, orient="vertical").pack(side="left", fill="y", padx=10)
        self.run_button = ttk.Button(actions, text="Tạo giọng + ghép vào video", command=self._start)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Dừng", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Label(actions, text="(không chọn dòng nào = chạy tất cả video; double-click = mở video đã xuất)").pack(
            side="left", padx=(10, 0)
        )

        srt_frame = ttk.LabelFrame(main, text="SRT của video đang chọn (dán text SRT vào đây)", padding=6)
        srt_frame.grid(row=5, column=0, sticky="nsew")
        srt_frame.columnconfigure(0, weight=1)
        srt_frame.rowconfigure(1, weight=1)
        bar = ttk.Frame(srt_frame)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="Dán từ clipboard", command=self._paste_clipboard).pack(side="left")
        ttk.Button(bar, text="Mở file .srt...", command=self._open_srt_file).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Xóa SRT", command=self._clear_srt).pack(side="left", padx=(6, 0))
        ttk.Label(bar, textvariable=self.srt_info_var).pack(side="left", padx=(12, 0))
        self.srt_text = scrolledtext.ScrolledText(srt_frame, wrap="word", height=10, undo=True)
        self.srt_text.grid(row=1, column=0, sticky="nsew")
        self.srt_text.bind("<KeyRelease>", self._on_text_changed)
        self.srt_text.bind("<<Paste>>", lambda _e: self.window.after(30, self._on_text_changed))
        self.srt_text.bind("<FocusOut>", self._on_text_changed)
        self.srt_text.configure(state="disabled")

        ttk.Label(main, textvariable=self.status_var).grid(row=6, column=0, sticky="w", pady=(6, 0))

    def _build_finish(self, main: ttk.Frame) -> None:
        box = ttk.LabelFrame(main, text="Sau khi ghép giọng: cắt khoảng lặng, khung xuất, nhạc nền", padding=6)
        box.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        trim = ttk.Frame(box)
        trim.pack(fill="x")
        ttk.Checkbutton(trim, text="Cắt bớt khoảng lặng giữa 2 câu", variable=self.trim_var).pack(side="left")
        ttk.Label(trim, text="Giữ lại (ms)").pack(side="left", padx=(12, 4))
        ttk.Spinbox(trim, textvariable=self.keep_gap_var, from_=0, to=2000, increment=50, width=6).pack(side="left")
        ttk.Label(trim, text="Chỉ cắt khi lặng dài hơn (ms)").pack(side="left", padx=(12, 4))
        ttk.Spinbox(trim, textvariable=self.min_silence_var, from_=100, to=10000, increment=50, width=6).pack(
            side="left"
        )
        ttk.Label(trim, text="Ngưỡng (dB)").pack(side="left", padx=(12, 4))
        ttk.Spinbox(trim, textvariable=self.noise_var, from_=-80, to=-10, increment=5, width=5).pack(side="left")
        ttk.Checkbutton(trim, text="Cắt cả đầu/cuối", variable=self.trim_edges_var).pack(side="left", padx=(12, 0))

        detect = ttk.Frame(box)
        detect.pack(fill="x", pady=(4, 0))
        ttk.Label(detect, text="Dò khoảng lặng theo").pack(side="left")
        ttk.Combobox(
            detect, textvariable=self.detect_var, values=list(DETECT_LABELS.values()), state="readonly", width=40
        ).pack(side="left", padx=(4, 0))
        ttk.Label(
            detect, text="(video gốc có tiếng/demo cần giữ thì để 'sau khi ghép'; chỉ cắt khi cả 2 cùng im)"
        ).pack(side="left", padx=(8, 0))

        layout = ttk.Frame(box)
        layout.pack(fill="x", pady=(4, 0))
        ttk.Label(layout, text="Khung xuất").pack(side="left")
        ttk.Combobox(
            layout, textvariable=self.layout_var, values=list(LAYOUT_LABELS.values()), state="readonly", width=44
        ).pack(side="left", padx=(4, 0))
        ttk.Button(layout, text="Áp dụng cho dòng chọn", command=self._apply_layout).pack(side="left", padx=(6, 0))
        ttk.Label(layout, text="Tự động: dọc nếu bản cuối ≤").pack(side="left", padx=(12, 4))
        ttk.Spinbox(layout, textvariable=self.short_limit_var, from_=10, to=600, increment=10, width=5).pack(
            side="left"
        )
        ttk.Label(layout, text="giây").pack(side="left", padx=(4, 0))

        music = ttk.Frame(box)
        music.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(music, text="Nhạc nền", variable=self.music_var).pack(side="left")
        ttk.Entry(music, textvariable=self.music_path_var, width=40).pack(side="left", padx=(4, 0), fill="x", expand=True)
        ttk.Button(music, text="Chọn...", command=self._choose_music).pack(side="left", padx=(6, 0))
        ttk.Label(music, text="Âm lượng %").pack(side="left", padx=(12, 4))
        ttk.Spinbox(music, textvariable=self.music_volume_var, from_=0, to=200, increment=5, width=5).pack(side="left")
        ttk.Checkbutton(music, text="Hạ nhạc khi có tiếng nói", variable=self.music_duck_var).pack(
            side="left", padx=(12, 0)
        )
        ttk.Label(music, text="Fade in/out (s)").pack(side="left", padx=(12, 4))
        ttk.Spinbox(music, textvariable=self.music_fade_in_var, from_=0, to=10, increment=0.5, width=4).pack(
            side="left"
        )
        ttk.Spinbox(music, textvariable=self.music_fade_out_var, from_=0, to=10, increment=0.5, width=4).pack(
            side="left", padx=(4, 0)
        )
        ttk.Label(
            box,
            text="Nhạc ngắn hơn video sẽ tự lặp lại. Bản dọc lưu tên *_voiced_9x16.mp4. "
            "Áp dụng khung: không chọn dòng nào = đổi tất cả.",
        ).pack(
            anchor="w", pady=(4, 0)
        )

    # ---------- settings ----------
    def _load_settings(self) -> None:
        data = self.settings.load()
        other = SettingsStore(TTS_TOOL_SETTINGS_PATH).load()
        self.remember_key_var.set(bool(data.get("remember_api_key", False)))
        saved_key = str(data.get("api_key", "")) if self.remember_key_var.get() else ""
        # Share the key/endpoint/voice already entered in the "Tự lên kịch bản + TTS" tool.
        self.api_key_var.set(
            saved_key
            or str(other.get("tts_api_key", "")).strip()
            or os.environ.get("TTSFORFREE_API_KEY", "").strip()
        )
        self.endpoint_var.set(
            str(data.get("endpoint", "")) or str(other.get("tts_endpoint", "")) or DEFAULT_ENDPOINT
        )
        self.voice_var.set(str(data.get("voice", "")) or str(other.get("voice", "")).strip())
        self.min_rate_var.set(str(data.get("min_rate", self.min_rate_var.get())))
        self.max_rate_var.set(str(data.get("max_rate", self.max_rate_var.get())))
        self.orig_volume_var.set(str(data.get("orig_volume", self.orig_volume_var.get())))
        self.voice_volume_var.set(str(data.get("voice_volume", self.voice_volume_var.get())))
        self.duck_var.set(bool(data.get("duck", True)))
        self.reuse_var.set(bool(data.get("reuse", True)))
        self.parallel_var.set(str(data.get("parallel", self.parallel_var.get())))
        self.output_dir_var.set(str(data.get("output_dir", "")))
        self.trim_var.set(bool(data.get("trim", True)))
        for key, var in (
            ("keep_gap_ms", self.keep_gap_var),
            ("min_silence_ms", self.min_silence_var),
            ("noise_db", self.noise_var),
            ("short_limit", self.short_limit_var),
            ("music_path", self.music_path_var),
            ("music_volume", self.music_volume_var),
            ("music_fade_in", self.music_fade_in_var),
            ("music_fade_out", self.music_fade_out_var),
        ):
            var.set(str(data.get(key, var.get())))
        self.trim_edges_var.set(bool(data.get("trim_edges", False)))
        self.detect_var.set(DETECT_LABELS.get(str(data.get("detect", "")), self.detect_var.get()))
        self.layout_var.set(LAYOUT_LABELS.get(str(data.get("layout", "")), self.layout_var.get()))
        self.music_var.set(bool(data.get("music", False)))
        self.music_duck_var.set(bool(data.get("music_duck", True)))

    def _save_settings(self) -> None:
        remember = self.remember_key_var.get()
        self.settings.save(
            {
                "endpoint": self.endpoint_var.get().strip(),
                "voice": self.voice_var.get().strip(),
                "min_rate": self.min_rate_var.get().strip(),
                "max_rate": self.max_rate_var.get().strip(),
                "orig_volume": self.orig_volume_var.get().strip(),
                "voice_volume": self.voice_volume_var.get().strip(),
                "duck": self.duck_var.get(),
                "reuse": self.reuse_var.get(),
                "parallel": self.parallel_var.get().strip(),
                "output_dir": self.output_dir_var.get().strip(),
                "trim": self.trim_var.get(),
                "keep_gap_ms": self.keep_gap_var.get().strip(),
                "min_silence_ms": self.min_silence_var.get().strip(),
                "noise_db": self.noise_var.get().strip(),
                "trim_edges": self.trim_edges_var.get(),
                "detect": self._label_key(DETECT_LABELS, self.detect_var, DETECT_MIX),
                "layout": self._label_key(LAYOUT_LABELS, self.layout_var, LAYOUT_ORIGINAL),
                "short_limit": self.short_limit_var.get().strip(),
                "music": self.music_var.get(),
                "music_path": self.music_path_var.get().strip(),
                "music_volume": self.music_volume_var.get().strip(),
                "music_duck": self.music_duck_var.get(),
                "music_fade_in": self.music_fade_in_var.get().strip(),
                "music_fade_out": self.music_fade_out_var.get().strip(),
                "remember_api_key": remember,
                "api_key": self.api_key_var.get().strip() if remember else "",
            }
        )

    @staticmethod
    def _label_key(labels: dict[str, str], var: tk.StringVar, default: str) -> str:
        return next((key for key, text in labels.items() if text == var.get()), default)

    def _choose_music(self) -> None:
        path = filedialog.askopenfilename(parent=self.window, title="Chọn nhạc nền", filetypes=MUSIC_TYPES)
        if path:
            self.music_path_var.set(path)
            self.music_var.set(True)

    def _apply_layout(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Đang chạy", "Dừng queue trước khi đổi khung.", parent=self.window)
            return
        layout = self._label_key(LAYOUT_LABELS, self.layout_var, LAYOUT_ORIGINAL)
        ids = list(self.tree.selection()) or list(self.tree.get_children())
        for iid in ids:
            self.items[iid].layout = layout
            self._refresh_row(iid)
        self.status_var.set(f"Đã đặt khung '{LAYOUT_SHORT[layout]}' cho {len(ids)} video.")

    # ---------- list handling ----------
    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.window, title="Thư mục lưu video đã ghép giọng")
        if path:
            self.output_dir_var.set(path)

    def _add_videos(self) -> None:
        paths = filedialog.askopenfilenames(parent=self.window, title="Chọn video", filetypes=VIDEO_TYPES)
        first_new: str | None = None
        for raw in paths:
            path = Path(raw).resolve()
            iid = str(path)
            if iid in self.items:
                continue
            item = VideoItem(path=path, layout=self._label_key(LAYOUT_LABELS, self.layout_var, LAYOUT_ORIGINAL))
            sidecar = path.with_suffix(".srt")
            if sidecar.is_file():
                try:
                    item.srt_text = read_text_file(sidecar)[0].strip()
                    item.status = "Đã nạp SRT cùng tên"
                except OSError:
                    pass
            self.items[iid] = item
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(str(path), cue_summary(item.srt_text), LAYOUT_SHORT[item.layout], item.status),
            )
            first_new = first_new or iid
        if first_new and not self.tree.selection():
            self.tree.selection_set(first_new)

    def _refresh_row(self, iid: str) -> None:
        if self.tree.exists(iid) and iid in self.items:
            item = self.items[iid]
            self.tree.item(
                iid,
                values=(str(item.path), cue_summary(item.srt_text), LAYOUT_SHORT[item.layout], item.status),
            )

    def _busy(self) -> bool:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Đang chạy", "Dừng queue trước khi xóa video.", parent=self.window)
            return True
        return False

    def _remove_selected(self) -> None:
        if self._busy():
            return
        self._commit_text()
        self.current_iid = None
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.tree.delete(iid)
        self._load_text(None)

    def _clear(self) -> None:
        if self._busy():
            return
        self.current_iid = None
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self._load_text(None)

    def _open_output(self, _event: object = None) -> None:
        selection = self.tree.selection()
        item = self.items.get(selection[0]) if selection else None
        if item and item.output and item.output.is_file():
            try:
                os.startfile(str(item.output))  # type: ignore[attr-defined]
            except AttributeError:
                self.status_var.set(f"Video đã xuất: {item.output}")
            except OSError as exc:
                messagebox.showerror("Không mở được", str(exc), parent=self.window)

    # ---------- SRT text of the selected video ----------
    def _commit_text(self) -> None:
        """Store what is typed in the text box into the video it belongs to."""
        if self.current_iid and self.current_iid in self.items:
            text = self.srt_text.get("1.0", "end-1c")
            item = self.items[self.current_iid]
            if text != item.srt_text:
                item.srt_text = text
                self._refresh_row(self.current_iid)

    def _load_text(self, iid: str | None) -> None:
        self.current_iid = iid
        self.srt_text.configure(state="normal")
        self.srt_text.delete("1.0", "end")
        if iid and iid in self.items:
            self.srt_text.insert("1.0", self.items[iid].srt_text)
            self.srt_info_var.set(f"{self.items[iid].path.name}: {cue_summary(self.items[iid].srt_text)}")
        else:
            self.srt_text.configure(state="disabled")
            self.srt_info_var.set("Chọn một video để dán SRT.")

    def _on_select(self, _event: object = None) -> None:
        self._commit_text()
        selection = self.tree.selection()
        target = selection[0] if selection else None
        if target != self.current_iid or target is None:
            self._load_text(target)

    def _on_text_changed(self, _event: object = None) -> None:
        self._commit_text()
        if self.current_iid and self.current_iid in self.items:
            item = self.items[self.current_iid]
            self.srt_info_var.set(f"{item.path.name}: {cue_summary(item.srt_text)}")

    def _require_selected(self) -> bool:
        if self.current_iid and self.current_iid in self.items:
            return True
        messagebox.showinfo("Chưa chọn video", "Chọn một video trong danh sách trước.", parent=self.window)
        return False

    def _paste_clipboard(self) -> None:
        if not self._require_selected():
            return
        try:
            text = self.window.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Clipboard trống", "Không có text trong clipboard.", parent=self.window)
            return
        self.srt_text.delete("1.0", "end")
        self.srt_text.insert("1.0", text.strip())
        self._on_text_changed()

    def _open_srt_file(self) -> None:
        if not self._require_selected():
            return
        raw = filedialog.askopenfilename(
            parent=self.window, title="Chọn file SRT", filetypes=(("SRT", "*.srt"), ("All files", "*.*"))
        )
        if not raw:
            return
        try:
            text = read_text_file(raw)[0]
        except OSError as exc:
            messagebox.showerror("Không đọc được file", str(exc), parent=self.window)
            return
        self.srt_text.delete("1.0", "end")
        self.srt_text.insert("1.0", text.strip())
        self._on_text_changed()

    def _clear_srt(self) -> None:
        if not self._require_selected():
            return
        self.srt_text.delete("1.0", "end")
        self._on_text_changed()

    # ---------- running ----------
    def _read_config(self) -> dict[str, Any]:
        api_key = self.api_key_var.get().strip()
        if not api_key:
            raise ValueError("Nhập TTS For Free API key.")
        voice = self.voice_var.get().strip()
        if not voice:
            raise ValueError("Nhập giọng (voice) của TTS For Free.")
        try:
            min_rate, max_rate = clamp_speech_rates(
                float(self.min_rate_var.get()), float(self.max_rate_var.get())
            )
            orig_volume = float(self.orig_volume_var.get()) / 100
            voice_volume = float(self.voice_volume_var.get()) / 100
            parallel = int(self.parallel_var.get())
        except ValueError:
            raise ValueError("Tốc độ đọc, âm lượng và số video song song phải là số.") from None
        if min(orig_volume, voice_volume) < 0:
            raise ValueError("Âm lượng không được âm.")
        try:
            keep_gap = float(self.keep_gap_var.get()) / 1000
            min_silence = float(self.min_silence_var.get()) / 1000
            noise_db = float(self.noise_var.get())
            short_limit = float(self.short_limit_var.get())
            music_volume = float(self.music_volume_var.get()) / 100
            fade_in = float(self.music_fade_in_var.get())
            fade_out = float(self.music_fade_out_var.get())
        except ValueError:
            raise ValueError("Thông số cắt khoảng lặng / khung / nhạc nền phải là số.") from None
        if self.trim_var.get() and (keep_gap < 0 or min_silence <= keep_gap):
            raise ValueError("'Chỉ cắt khi lặng dài hơn' phải lớn hơn 'Giữ lại'.")
        music = None
        if self.music_var.get():
            music_path = Path(self.music_path_var.get().strip().strip('"'))
            if not music_path.is_file():
                raise ValueError("Đã tick Nhạc nền nhưng chưa chọn file nhạc (hoặc file không tồn tại).")
            music = MusicOptions(
                path=music_path,
                volume=max(0.0, music_volume),
                duck=self.music_duck_var.get(),
                fade_in=max(0.0, fade_in),
                fade_out=max(0.0, fade_out),
            )
        return {
            "endpoint": self.endpoint_var.get().strip(),
            "api_key": api_key,
            "voice": voice,
            "min_rate": min_rate,
            "max_rate": max_rate,
            "options": MixOptions(
                original_volume=orig_volume,
                voice_volume=voice_volume,
                duck_original=self.duck_var.get(),
            ),
            "reuse": self.reuse_var.get(),
            "parallel": max(1, min(MAX_PARALLEL_VIDEOS, parallel)),
            "output_dir": self.output_dir_var.get().strip(),
            "trim": (
                TrimOptions(
                    noise_db=min(-1.0, noise_db),
                    min_silence=min_silence,
                    keep_gap=keep_gap,
                    trim_edges=self.trim_edges_var.get(),
                )
                if self.trim_var.get()
                else None
            ),
            "detect": self._label_key(DETECT_LABELS, self.detect_var, DETECT_MIX),
            "short_limit": short_limit,
            "music": music,
        }

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._commit_text()
        try:
            config = self._read_config()
            ids = list(self.tree.selection()) or list(self.tree.get_children())
            if not ids:
                raise ValueError("Thêm video trước.")
            ensure_ffmpeg()
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Chưa chạy được", str(exc), parent=self.window)
            return
        # Snapshot so edits during the run can't change what is being voiced.
        jobs = [
            (iid, self.items[iid].path, self.items[iid].srt_text, self.items[iid].layout) for iid in ids
        ]
        for iid, *_rest in jobs:
            self.items[iid].status = "Đang chờ..."
            self._refresh_row(iid)
        self.cancel_event.clear()
        self._set_busy(True)
        self.worker = threading.Thread(target=self._run_queue, args=(jobs, config), daemon=True)
        self.worker.start()

    def _run_queue(self, jobs: list[tuple[str, Path, str, str]], config: dict[str, Any]) -> None:
        failed = 0
        total = len(jobs)
        counter = {"done": 0}
        lock = threading.Lock()

        def run_one(job: tuple[str, Path, str, str]) -> bool:
            iid, path, srt_text, layout = job
            if self.cancel_event.is_set():
                self.events.put(("row", (iid, "Đã dừng")))
                return True
            try:
                output, note = self._process(iid, path, srt_text, layout, config)
            except InterruptedError:
                self.events.put(("row", (iid, "Đã dừng")))
                return True
            except Exception as exc:
                traceback.print_exc()
                self.events.put(("row", (iid, f"Lỗi: {exc}")))
                return False
            self.events.put(("output", (iid, output, "Xong" + (f" ({note})" if note else ""))))
            return True
        try:
            with ThreadPoolExecutor(max_workers=config["parallel"]) as pool:
                for ok in pool.map(run_one, jobs):
                    with lock:
                        counter["done"] += 1
                        failed += 0 if ok else 1
                    self.events.put(("status", f"Đã xử lý {counter['done']}/{total} video."))
            self.events.put(("stopped" if self.cancel_event.is_set() else "done", failed))
        except Exception as exc:
            traceback.print_exc()
            self.events.put(("error", str(exc)))

    def _process(
        self, iid: str, video: Path, srt_text: str, layout: str, config: dict[str, Any]
    ) -> tuple[Path, str]:
        def step(message: str) -> None:
            self.events.put(("row", (iid, message)))

        if not srt_text.strip():
            raise RuntimeError("Chưa có SRT cho video này.")
        cues = parse_srt(srt_text)
        if not cues:
            raise RuntimeError("SRT không đúng định dạng (cần dòng 00:00:00,000 --> 00:00:00,000).")
        step("Đang đọc video...")
        info = probe_media(video)
        if info.duration <= 0:
            raise RuntimeError("Không đọc được độ dài video.")
        note = ""
        if cues[-1].end > info.duration + 0.5:
            note = "SRT dài hơn video, phần thừa bị cắt"

        out_dir = Path(config["output_dir"]) if config["output_dir"] else video.parent
        work_dir = out_dir / WORK_DIR_NAME
        work_dir.mkdir(parents=True, exist_ok=True)
        stem = video.stem
        (work_dir / f"{stem}.srt").write_text(srt_text.strip() + "\n", encoding="utf-8-sig")

        fingerprint = srt_fingerprint(
            cues,
            config["voice"],
            {"min": config["min_rate"], "max": config["max_rate"], "speed": DEFAULT_BLOCK_SPEED},
        )
        meta_path = work_dir / f"{stem}_voice.json"
        voice_audio = self._reusable_voice(work_dir, stem, meta_path, fingerprint) if config["reuse"] else None
        if voice_audio:
            step("Dùng lại giọng đã tạo (không tốn credit)")
        else:
            step("Đang tạo giọng (TTS For Free)...")
            client = TtsForFreeSrtClient(
                config["endpoint"],
                config["api_key"],
                lambda message: self.events.put(("row", (iid, message))),
                self.cancel_event,
            )
            for stale in work_dir.glob(f"{stem}_voice.*"):
                if stale.suffix != ".json":
                    stale.unlink(missing_ok=True)
            voice_audio = client.synthesize(
                cues,
                config["voice"],
                work_dir / f"{stem}_voice",
                speed=DEFAULT_BLOCK_SPEED,
                min_rate=config["min_rate"],
                max_rate=config["max_rate"],
            )
            meta_path.write_text(
                json.dumps({"fingerprint": fingerprint, "audio": voice_audio.name}, indent=2),
                encoding="utf-8",
            )
        step("Đang ghép giọng vào video...")
        final = out_dir / f"{stem}{OUTPUT_SUFFIX}.mp4"
        if config["trim"] is None and config["music"] is None and layout == LAYOUT_ORIGINAL:
            output = mix_voiceover(video, voice_audio, final, info, config["options"], self.cancel_event)
            return output, note
        raw = mix_voiceover(
            video, voice_audio, work_dir / f"{stem}_voiced_raw.mp4", info, config["options"], self.cancel_event
        )
        try:
            output, finish_note = self._finish(iid, raw, voice_audio, final, layout, config)
        finally:
            raw.unlink(missing_ok=True)
        return output, ", ".join(part for part in (note, finish_note) if part)

    def _finish(
        self, iid: str, raw: Path, voice_audio: Path, final: Path, layout: str, config: dict[str, Any]
    ) -> tuple[Path, str]:
        """Shorten pauses, reframe and add background music to the mixed video in one encode."""
        def step(message: str) -> None:
            self.events.put(("row", (iid, message)))

        raw_info = probe_media(raw)
        fps = raw_info.fps or DEFAULT_FPS
        notes: list[str] = []
        if config["trim"] is not None:
            step("Đang tìm khoảng lặng...")
            if config["detect"] == DETECT_VOICE:
                source, source_duration = voice_audio, probe_media(voice_audio).duration
            else:
                source, source_duration = raw, raw_info.duration
            # The voice can run past the video end; that part is already cut off in raw.
            plan = analyze(
                source,
                config["trim"],
                min(source_duration, raw_info.duration),
                raw_info.duration,
                fps,
            )
            notes.append(
                f"cắt {len(plan.cuts)} khoảng lặng, {clock(plan.duration)} → {clock(plan.new_duration)}"
            )
        else:
            plan = no_cut_plan(raw_info.duration)
        final_layout = resolve_layout(layout, plan.new_duration, config["short_limit"])
        if final_layout != LAYOUT_ORIGINAL:
            final = final.with_name(final.stem + VERTICAL_SUFFIX + final.suffix)
            notes.append("dọc 9:16")
        if config["music"] is not None:
            notes.append("có nhạc nền")
        if not plan.cuts and config["music"] is None and final_layout == LAYOUT_ORIGINAL:
            final.unlink(missing_ok=True)
            raw.replace(final)
            return final, ", ".join(notes)
        step("Đang dựng bản cuối...")
        render_trimmed(
            plan,
            None,
            final,
            video=raw,
            video_duration=raw_info.duration,
            fps=fps,
            layout=final_layout,
            music=config["music"],
            on_progress=lambda value: step(f"Đang dựng bản cuối {value * 100:.0f}%"),
            cancel_event=self.cancel_event,
        )
        return final, ", ".join(notes)

    @staticmethod
    def _reusable_voice(work_dir: Path, stem: str, meta_path: Path, fingerprint: str) -> Path | None:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or meta.get("fingerprint") != fingerprint:
            return None
        audio = work_dir / str(meta.get("audio", ""))
        return audio if audio.is_file() and audio.stat().st_size > 0 else None

    def _flush_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "row":
                    iid, text = payload
                    if iid in self.items:
                        self.items[iid].status = text
                        self._refresh_row(iid)
                elif kind == "output":
                    iid, path, text = payload
                    if iid in self.items:
                        self.items[iid].output = path
                        self.items[iid].status = text
                        self._refresh_row(iid)
                elif kind == "done":
                    self._set_busy(False)
                    self.status_var.set(
                        "Xong queue."
                        + (f" {payload} video lỗi (xem cột trạng thái)." if payload else "")
                    )
                elif kind == "stopped":
                    self._set_busy(False)
                    self.status_var.set("Đã dừng.")
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set("Lỗi.")
                    messagebox.showerror("Lỗi", str(payload), parent=self.window)
        except queue.Empty:
            pass
        self.window.after(120, self._flush_events)

    def _set_busy(self, busy: bool) -> None:
        self.run_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

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
    VoiceoverWindow(root)
    root.mainloop()
