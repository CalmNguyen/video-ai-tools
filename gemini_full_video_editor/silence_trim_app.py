from __future__ import annotations

import os
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
    DEFAULT_SHORT_LIMIT,
    LAYOUT_AUTO,
    LAYOUT_LABELS,
    LAYOUT_ORIGINAL,
    LAYOUT_SHORT,
    VERTICAL_SUFFIX,
    TrimOptions,
    TrimPlan,
    analyze,
    render_trimmed,
    resolve_layout,
)
from .srt_utils import format_timestamp


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / "silence_trimmer.settings.json"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
MEDIA_TYPES = (
    ("MP3 + MP4", " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTS | VIDEO_EXTS))),
    ("Audio", " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTS))),
    ("Video", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTS))),
    ("All files", "*.*"),
)
AUDIO_TYPES = (("Audio", " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTS))), ("All files", "*.*"))
VIDEO_TYPES = (("Video", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTS))), ("All files", "*.*"))
OUTPUT_SUFFIX = "_trimmed"
MAX_PARALLEL = 8

def short_time(seconds: float) -> str:
    text = format_timestamp(seconds)
    return text[3:] if text.startswith("00:") else text


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def pair_key(path: Path) -> str:
    return path.stem.strip().lower()


@dataclass(frozen=True)
class Job:
    iid: str
    audio: Path | None
    video: Path | None
    # Output path without extension, e.g. out/S08_en_trimmed.
    base: Path
    layout: str


@dataclass
class TrimItem:
    audio: Path | None = None
    video: Path | None = None
    layout: str = LAYOUT_AUTO
    status: str = "-"
    plan: TrimPlan | None = None
    outputs: tuple[Path, ...] = ()

    @property
    def name(self) -> str:
        return (self.video or self.audio or Path("?")).stem

    def summary(self) -> str:
        if self.plan is None:
            return ""
        return f"{len(self.plan.cuts)} chỗ, {clock(self.plan.duration)} → {clock(self.plan.new_duration)}"


class SilenceTrimWindow:
    """Batch: shorten long pauses in each voice MP3 and cut its MP4 at the same places."""

    def __init__(self, window: tk.Tk | tk.Toplevel):
        self.window = window
        self.window.title("Cắt khoảng lặng MP3 + cắt MP4 theo (nhiều cặp)")
        self.window.geometry("1180x800")
        self.window.minsize(920, 640)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closed = False
        self.items: dict[str, TrimItem] = {}
        self.next_id = 0
        self.settings = SettingsStore(SETTINGS_PATH)

        self.output_dir_var = tk.StringVar()
        self.noise_var = tk.StringVar(value=f"{DEFAULT_NOISE_DB:g}")
        self.min_silence_var = tk.StringVar(value=str(DEFAULT_MIN_SILENCE_MS))
        self.keep_gap_var = tk.StringVar(value=str(DEFAULT_KEEP_GAP_MS))
        self.trim_edges_var = tk.BooleanVar(value=False)
        self.export_mp3_var = tk.BooleanVar(value=True)
        self.parallel_var = tk.StringVar(value="3")
        self.layout_var = tk.StringVar(value=LAYOUT_LABELS[LAYOUT_AUTO])
        self.short_limit_var = tk.StringVar(value=str(DEFAULT_SHORT_LIMIT))
        self.detail_var = tk.StringVar(value="Chọn một dòng để xem các đoạn sẽ cắt.")
        self.status_var = tk.StringVar(
            value="Thêm file MP3 + MP4 (chọn nhiều cùng lúc, tự ghép theo tên), rồi Phân tích hoặc Cắt + xuất."
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
        main.rowconfigure(2, weight=3)
        main.rowconfigure(4, weight=2)

        opts = ttk.LabelFrame(main, text="Khoảng lặng & xuất (áp dụng cho mọi cặp)", padding=6)
        opts.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        opts.columnconfigure(7, weight=1)
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
            row=0, column=5, padx=(4, 16)
        )
        ttk.Label(opts, text="Chạy song song").grid(row=0, column=6, sticky="e")
        ttk.Spinbox(opts, textvariable=self.parallel_var, from_=1, to=MAX_PARALLEL, width=4).grid(
            row=0, column=7, sticky="w", padx=(4, 0)
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
        ).grid(row=1, column=4, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Thư mục xuất").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(opts, textvariable=self.output_dir_var).grid(
            row=2, column=1, columnspan=6, sticky="ew", padx=(4, 0), pady=(6, 0)
        )
        ttk.Button(opts, text="Chọn...", command=self._choose_output_dir).grid(
            row=2, column=7, sticky="w", padx=(8, 0), pady=(6, 0)
        )
        ttk.Label(opts, text="(để trống = lưu cạnh file gốc, tên *_trimmed.mp4, bản dọc *_trimmed_9x16.mp4)").grid(
            row=3, column=1, columnspan=7, sticky="w", padx=(4, 0)
        )
        ttk.Label(opts, text="Khung xuất").grid(row=4, column=0, sticky="w", pady=(6, 0))
        layout_row = ttk.Frame(opts)
        layout_row.grid(row=4, column=1, columnspan=7, sticky="w", padx=(4, 0), pady=(6, 0))
        ttk.Combobox(
            layout_row,
            textvariable=self.layout_var,
            values=list(LAYOUT_LABELS.values()),
            state="readonly",
            width=44,
        ).pack(side="left")
        ttk.Button(layout_row, text="Áp dụng cho dòng chọn", command=self._apply_layout).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(layout_row, text="Tự động: dọc nếu sau khi cắt ≤").pack(side="left", padx=(12, 4))
        ttk.Spinbox(layout_row, textvariable=self.short_limit_var, from_=10, to=600, increment=10, width=5).pack(
            side="left"
        )
        ttk.Label(layout_row, text="giây").pack(side="left", padx=(4, 0))
        ttk.Label(
            opts, text="(khung đang chọn được dùng cho file mới thêm; không chọn dòng nào khi Áp dụng = đổi tất cả)"
        ).grid(row=5, column=1, columnspan=7, sticky="w", padx=(4, 0))

        files = ttk.Frame(main)
        files.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(files, text="Thêm file MP3 + MP4...", command=self._add_files).pack(side="left")
        ttk.Button(files, text="Thêm cả thư mục...", command=self._add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(files, text="Đổi MP3 dòng chọn...", command=lambda: self._replace_file("audio")).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(files, text="Đổi MP4 dòng chọn...", command=lambda: self._replace_file("video")).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(files, text="Xóa đã chọn", command=self._remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(files, text="Xóa hết", command=self._clear).pack(side="left", padx=(6, 0))

        list_frame = ttk.Frame(main)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            list_frame,
            columns=("audio", "video", "layout", "cuts", "status"),
            show="headings",
            selectmode="extended",
            height=8,
        )
        for column, title, width in (
            ("audio", "MP3", 220),
            ("video", "MP4", 220),
            ("layout", "Khung", 190),
            ("cuts", "Cắt", 150),
            ("status", "Trạng thái", 320),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column in {"audio", "video", "status"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._open_output)

        actions = ttk.Frame(main)
        actions.grid(row=3, column=0, sticky="ew", pady=6)
        self.analyze_button = ttk.Button(actions, text="Phân tích khoảng lặng", command=lambda: self._start(False))
        self.analyze_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="Cắt + xuất", command=lambda: self._start(True))
        self.run_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(actions, text="Dừng", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Label(actions, text="(không chọn dòng nào = chạy tất cả; double-click = mở video đã xuất)").pack(
            side="left", padx=(10, 0)
        )
        self.progress = ttk.Progressbar(actions, mode="determinate", maximum=100, length=200)
        self.progress.pack(side="right")

        table = ttk.LabelFrame(main, text="Các đoạn sẽ cắt của dòng đang chọn (theo thời gian MP3 gốc)", padding=6)
        table.grid(row=4, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        ttk.Label(table, textvariable=self.detail_var).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.cut_tree = ttk.Treeview(table, columns=("index", "silence", "cut", "removed"), show="headings")
        for column, title, width in (
            ("index", "#", 50),
            ("silence", "Khoảng lặng gốc", 260),
            ("cut", "Đoạn bị cắt", 260),
            ("removed", "Bỏ", 100),
        ):
            self.cut_tree.heading(column, text=title)
            self.cut_tree.column(column, width=width, stretch=column in {"silence", "cut"})
        self.cut_tree.grid(row=1, column=0, sticky="nsew")
        cut_scroll = ttk.Scrollbar(table, orient="vertical", command=self.cut_tree.yview)
        cut_scroll.grid(row=1, column=1, sticky="ns")
        self.cut_tree.configure(yscrollcommand=cut_scroll.set)

        ttk.Label(main, textvariable=self.status_var).grid(row=5, column=0, sticky="w", pady=(6, 0))

    # ---------- settings ----------
    def _load_settings(self) -> None:
        data = self.settings.load()
        self.output_dir_var.set(str(data.get("output_dir", "")))
        self.noise_var.set(str(data.get("noise_db", self.noise_var.get())))
        self.min_silence_var.set(str(data.get("min_silence_ms", self.min_silence_var.get())))
        self.keep_gap_var.set(str(data.get("keep_gap_ms", self.keep_gap_var.get())))
        self.trim_edges_var.set(bool(data.get("trim_edges", False)))
        self.export_mp3_var.set(bool(data.get("export_mp3", True)))
        self.parallel_var.set(str(data.get("parallel", self.parallel_var.get())))
        self.layout_var.set(LAYOUT_LABELS.get(str(data.get("layout", "")), self.layout_var.get()))
        self.short_limit_var.set(str(data.get("short_limit", self.short_limit_var.get())))

    def _save_settings(self) -> None:
        self.settings.save(
            {
                "output_dir": self.output_dir_var.get().strip(),
                "noise_db": self.noise_var.get().strip(),
                "min_silence_ms": self.min_silence_var.get().strip(),
                "keep_gap_ms": self.keep_gap_var.get().strip(),
                "trim_edges": self.trim_edges_var.get(),
                "export_mp3": self.export_mp3_var.get(),
                "parallel": self.parallel_var.get().strip(),
                "layout": self._selected_layout(),
                "short_limit": self.short_limit_var.get().strip(),
            }
        )

    # ---------- list handling ----------
    def _selected_layout(self) -> str:
        label = self.layout_var.get()
        return next((key for key, text in LAYOUT_LABELS.items() if text == label), LAYOUT_AUTO)

    def _apply_layout(self) -> None:
        if self._busy():
            return
        layout = self._selected_layout()
        ids = list(self.tree.selection()) or list(self.tree.get_children())
        for iid in ids:
            self.items[iid].layout = layout
            self._refresh_row(iid)
        self.status_var.set(f"Đã đặt khung '{LAYOUT_SHORT[layout]}' cho {len(ids)} dòng.")

    def _layout_text(self, item: TrimItem) -> str:
        text = LAYOUT_SHORT.get(item.layout, item.layout)
        if item.layout == LAYOUT_AUTO and item.plan is not None:
            try:
                limit = float(self.short_limit_var.get())
            except ValueError:
                limit = DEFAULT_SHORT_LIMIT
            text += " → " + LAYOUT_SHORT[resolve_layout(item.layout, item.plan.new_duration, limit)]
        return text

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.window, title="Thư mục lưu file đã cắt")
        if path:
            self.output_dir_var.set(path)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.window, title="Chọn các file MP3 và MP4 (cùng tên sẽ tự ghép cặp)", filetypes=MEDIA_TYPES
        )
        self._add_paths([Path(raw) for raw in paths])

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.window, title="Thư mục chứa MP3 + MP4 cùng tên")
        if folder:
            self._add_paths(sorted(Path(folder).iterdir()))

    def _add_paths(self, paths: list[Path]) -> None:
        if self._busy():
            return
        known = {
            str(path)
            for item in self.items.values()
            for path in (item.audio, item.video)
            if path is not None
        }
        added = 0
        for raw in paths:
            path = raw.resolve()
            ext = path.suffix.lower()
            kind = "audio" if ext in AUDIO_EXTS else "video" if ext in VIDEO_EXTS else ""
            if not kind or not path.is_file() or str(path) in known:
                continue
            known.add(str(path))
            # Pair with a row that has the other half and the same file name.
            target = next(
                (
                    iid
                    for iid, item in self.items.items()
                    if getattr(item, kind) is None
                    and (other := item.video if kind == "audio" else item.audio) is not None
                    and pair_key(other) == pair_key(path)
                ),
                None,
            )
            if target is None:
                target = self._new_row()
                self.items[target].layout = self._selected_layout()
                added += 1
            setattr(self.items[target], kind, path)
            self.items[target].plan = None
            self._refresh_row(target)
        missing = sum(1 for item in self.items.values() if item.audio is None)
        note = f" {missing} dòng chưa có MP3 (bấm Đổi MP3 dòng chọn)." if missing else ""
        self.status_var.set(f"Đang có {len(self.items)} cặp (thêm {added}).{note}")

    def _new_row(self) -> str:
        self.next_id += 1
        iid = f"row{self.next_id}"
        self.items[iid] = TrimItem()
        self.tree.insert("", "end", iid=iid, values=("", "", "", "", "-"))
        return iid

    def _replace_file(self, kind: str) -> None:
        if self._busy():
            return
        selection = self.tree.selection()
        if len(selection) != 1:
            messagebox.showinfo("Chọn một dòng", "Chọn đúng một dòng trong danh sách trước.", parent=self.window)
            return
        raw = filedialog.askopenfilename(
            parent=self.window,
            title="Chọn MP3" if kind == "audio" else "Chọn MP4",
            filetypes=AUDIO_TYPES if kind == "audio" else VIDEO_TYPES,
        )
        if raw:
            item = self.items[selection[0]]
            setattr(item, kind, Path(raw).resolve())
            item.plan = None
            item.status = "-"
            self._refresh_row(selection[0])
            self._show_detail(selection[0])

    def _refresh_row(self, iid: str) -> None:
        if self.tree.exists(iid) and iid in self.items:
            item = self.items[iid]
            self.tree.item(
                iid,
                values=(
                    item.audio.name if item.audio else "(thiếu MP3)",
                    item.video.name if item.video else "(không có MP4: chỉ cắt MP3)",
                    self._layout_text(item) if item.video else "-",
                    item.summary(),
                    item.status,
                ),
            )

    def _busy(self) -> bool:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Đang chạy", "Dừng queue trước khi đổi danh sách.", parent=self.window)
            return True
        return False

    def _remove_selected(self) -> None:
        if self._busy():
            return
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.tree.delete(iid)
        self._show_detail(None)

    def _clear(self) -> None:
        if self._busy():
            return
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self._show_detail(None)

    def _on_select(self, _event: object = None) -> None:
        selection = self.tree.selection()
        self._show_detail(selection[0] if selection else None)

    def _show_detail(self, iid: str | None) -> None:
        self.cut_tree.delete(*self.cut_tree.get_children())
        item = self.items.get(iid) if iid else None
        if item is None:
            self.detail_var.set("Chọn một dòng để xem các đoạn sẽ cắt.")
            return
        plan = item.plan
        if plan is None:
            self.detail_var.set(f"{item.name}: chưa phân tích.")
            return
        for index, ((start, end), (cut_start, cut_end)) in enumerate(
            zip(plan.cut_sources, plan.cuts), start=1
        ):
            self.cut_tree.insert(
                "",
                "end",
                values=(
                    index,
                    f"{short_time(start)} → {short_time(end)}  ({end - start:.2f}s)",
                    f"{short_time(cut_start)} → {short_time(cut_end)}",
                    f"{cut_end - cut_start:.2f}s",
                ),
            )
        self.detail_var.set(
            f"{item.name}: {len(plan.cuts)} chỗ cắt, bỏ {plan.removed:.1f}s: "
            f"{short_time(plan.duration)} → {short_time(plan.new_duration)}"
        )

    def _open_output(self, _event: object = None) -> None:
        selection = self.tree.selection()
        item = self.items.get(selection[0]) if selection else None
        output = next((path for path in item.outputs if path.is_file()), None) if item else None
        if output is None:
            return
        try:
            os.startfile(str(output))  # type: ignore[attr-defined]
        except AttributeError:
            self.status_var.set(f"File đã xuất: {output}")
        except OSError as exc:
            messagebox.showerror("Không mở được", str(exc), parent=self.window)

    # ---------- running ----------
    def _read_config(self) -> dict[str, Any]:
        try:
            noise_db = float(self.noise_var.get())
            min_silence = float(self.min_silence_var.get()) / 1000
            keep_gap = float(self.keep_gap_var.get()) / 1000
            parallel = int(self.parallel_var.get())
            short_limit = float(self.short_limit_var.get())
        except ValueError:
            raise ValueError("Ngưỡng dB, thời gian và số chạy song song phải là số.") from None
        if keep_gap < 0 or min_silence <= 0:
            raise ValueError("Thời gian phải lớn hơn 0.")
        if min_silence <= keep_gap:
            raise ValueError("'Chỉ cắt khi lặng dài hơn' phải lớn hơn 'Giữ lại giữa 2 câu'.")
        return {
            "output_dir": self.output_dir_var.get().strip(),
            "export_mp3": self.export_mp3_var.get(),
            "parallel": max(1, min(MAX_PARALLEL, parallel)),
            "short_limit": short_limit,
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
            ids = list(self.tree.selection()) or list(self.tree.get_children())
            if not ids:
                raise ValueError("Thêm file MP3 + MP4 trước.")
            ensure_ffmpeg()
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Chưa chạy được", str(exc), parent=self.window)
            return
        jobs = self._build_jobs(ids, config)
        for job in jobs:
            self.items[job.iid].status = "Đang chờ..."
            self._refresh_row(job.iid)
        self.cancel_event.clear()
        self.progress.configure(value=0)
        self._set_busy(True)
        self.worker = threading.Thread(target=self._run_queue, args=(jobs, config, render), daemon=True)
        self.worker.start()

    def _build_jobs(self, ids: list[str], config: dict[str, Any]) -> list[Job]:
        """Snapshot each row (so edits during the run change nothing) with its output
        base name, numbering names that would collide in the same folder."""
        used: set[str] = set()
        result = []
        for iid in ids:
            item = self.items[iid]
            source = item.video or item.audio
            out_dir = Path(config["output_dir"]) if config["output_dir"] else (source.parent if source else Path("."))
            stem = f"{source.stem if source else iid}{OUTPUT_SUFFIX}"
            base, counter = out_dir / stem, 2
            while str(base).lower() in used:
                base, counter = out_dir / f"{stem}_{counter}", counter + 1
            used.add(str(base).lower())
            result.append(Job(iid, item.audio, item.video, base, item.layout))
        return result

    def _run_queue(self, jobs: list[Job], config: dict[str, Any], render: bool) -> None:
        total = len(jobs)
        lock = threading.Lock()
        fractions = {job.iid: 0.0 for job in jobs}
        state = {"done": 0, "failed": 0}

        def report(iid: str, fraction: float) -> None:
            with lock:
                fractions[iid] = fraction
                overall = sum(fractions.values()) / total * 100
            self.events.put(("progress", overall))

        def run_one(job: Job) -> None:
            iid = job.iid
            ok = True
            if self.cancel_event.is_set():
                self.events.put(("row", (iid, "Đã dừng")))
            else:
                try:
                    message = self._process(job, config, render, report)
                    self.events.put(("row", (iid, message)))
                except InterruptedError:
                    self.events.put(("row", (iid, "Đã dừng")))
                except Exception as exc:
                    traceback.print_exc()
                    self.events.put(("row", (iid, f"Lỗi: {exc}")))
                    ok = False
            report(iid, 1.0)
            with lock:
                state["done"] += 1
                state["failed"] += 0 if ok else 1
                done = state["done"]
            self.events.put(("status", f"Đã xử lý {done}/{total} cặp."))

        try:
            with ThreadPoolExecutor(max_workers=config["parallel"]) as pool:
                list(pool.map(run_one, jobs))
            if self.cancel_event.is_set():
                self.events.put(("stopped", None))
            else:
                self.events.put(("done", (state["failed"], render)))
        except Exception as exc:
            traceback.print_exc()
            self.events.put(("error", str(exc)))

    def _process(self, job: Job, config: dict[str, Any], render: bool, report: Any) -> str:
        iid, audio, video, base = job.iid, job.audio, job.video, job.base

        def step(message: str) -> None:
            self.events.put(("row", (iid, message)))

        if audio is None:
            raise RuntimeError("Chưa có MP3 cho dòng này.")
        step("Đang đọc file...")
        audio_info = probe_media(audio)
        if not audio_info.has_audio or audio_info.duration <= 0:
            raise RuntimeError("File MP3 không có âm thanh.")
        video_duration, fps = 0.0, DEFAULT_FPS
        if video is not None:
            video_info = probe_media(video)
            if video_info.duration <= 0 or not video_info.width:
                raise RuntimeError("Không đọc được video.")
            video_duration, fps = video_info.duration, video_info.fps or DEFAULT_FPS
        step("Đang tìm khoảng lặng...")
        plan = analyze(audio, config["options"], audio_info.duration, video_duration, fps)
        self.events.put(("plan", (iid, plan)))
        if not render:
            return f"Đã phân tích: bỏ {plan.removed:.1f}s"
        if not plan.cuts:
            return "Không có khoảng lặng nào cần cắt"

        layout = resolve_layout(job.layout, plan.new_duration, config["short_limit"])
        targets: list[tuple[Path, Path | None]] = []
        if video is not None:
            suffix = "" if layout == LAYOUT_ORIGINAL else VERTICAL_SUFFIX
            targets.append((base.with_name(base.name + suffix + ".mp4"), video))
        if video is None or config["export_mp3"]:
            targets.append((base.with_name(base.name + ".mp3"), None))
        outputs: list[Path] = []
        for index, (output, source_video) in enumerate(targets):
            label = "video" if source_video else "MP3"

            def on_progress(value: float, index: int = index, label: str = label) -> None:
                fraction = (index + value) / len(targets)
                report(iid, fraction)
                step(f"Đang cắt {label} {value * 100:.0f}%")

            step(f"Đang cắt {label}...")
            outputs.append(
                render_trimmed(
                    plan,
                    audio,
                    output,
                    video=source_video,
                    video_duration=video_duration,
                    fps=fps,
                    layout=layout,
                    on_progress=on_progress,
                    cancel_event=self.cancel_event,
                )
            )
        self.events.put(("outputs", (iid, tuple(outputs))))
        return "Xong: " + ", ".join(path.name for path in outputs)

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
                elif kind == "row":
                    iid, text = payload
                    if iid in self.items:
                        self.items[iid].status = text
                        self._refresh_row(iid)
                elif kind == "plan":
                    iid, plan = payload
                    if iid in self.items:
                        self.items[iid].plan = plan
                        self._refresh_row(iid)
                        if iid in self.tree.selection()[:1]:
                            self._show_detail(iid)
                elif kind == "outputs":
                    iid, outputs = payload
                    if iid in self.items:
                        self.items[iid].outputs = outputs
                elif kind == "done":
                    failed, render = payload
                    self._set_busy(False)
                    self.progress.configure(value=100)
                    self.status_var.set(
                        ("Xong, đã xuất file." if render else "Đã phân tích. Bấm Cắt + xuất để tạo file.")
                        + (f" {failed} cặp lỗi (xem cột trạng thái)." if failed else "")
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
        state = "disabled" if busy else "normal"
        self.analyze_button.configure(state=state)
        self.run_button.configure(state=state)
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
    SilenceTrimWindow(root)
    root.mainloop()
