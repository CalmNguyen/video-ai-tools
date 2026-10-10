from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .gemini_audio import transcribe_media
from .gemini_client import GeminiJsonClient
from .media_utils import ensure_ffmpeg, probe_media
from .settings import DEFAULT_MODELS, FLASH_MODELS, SettingsStore
from .srt_utils import read_srt, write_srt
from .video_metadata import LANGUAGE_AUTO, LANGUAGES, VideoMetadata, generate_video_metadata


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / "gemini_video_meta.settings.json"
EDITOR_SETTINGS_PATH = PACKAGE_ROOT / "gemini_ai_full_video_editor.settings.json"
MODELS = FLASH_MODELS + tuple(model for model in DEFAULT_MODELS if model not in FLASH_MODELS)
VIDEO_TYPES = (
    ("Video/Audio", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv *.mp3 *.wav *.m4a"),
    ("All files", "*.*"),
)

TASK_SRT = "srt"
TASK_META = "meta"


@dataclass
class VideoItem:
    path: Path
    srt_status: str = "-"
    meta_status: str = "-"
    srt_path: Path | None = None
    meta: VideoMetadata | None = None


class VideoMetaWindow:
    """Batch: get SRT from videos with Gemini, then write title/description/thumbnail prompt."""

    def __init__(self, window: tk.Tk | tk.Toplevel):
        self.window = window
        self.window.title("Video → SRT + Title/Description/Thumbnail (Gemini)")
        self.window.geometry("1180x780")
        self.window.minsize(900, 620)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closed = False
        self.items: dict[str, VideoItem] = {}
        self.settings = SettingsStore(SETTINGS_PATH)

        self.api_key_var = tk.StringVar()
        self.remember_key_var = tk.BooleanVar(value=False)
        self.srt_model_var = tk.StringVar(value=MODELS[0])
        self.meta_model_var = tk.StringVar(value=MODELS[0])
        self.language_var = tk.StringVar(value=LANGUAGE_AUTO)
        self.output_dir_var = tk.StringVar()
        self.overwrite_srt_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(
            value="Thêm video rồi chọn Lấy SRT, Tạo title/desc/thumbnail hoặc Xếp queue cả hai."
        )
        self.title_var = tk.StringVar()

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

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="API Key").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(top, textvariable=self.api_key_var, show="*").grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(top, text="Nhớ API key", variable=self.remember_key_var).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Label(top, text="Model lấy SRT").grid(row=1, column=0, sticky="w", pady=(6, 0))
        models = ttk.Frame(top)
        models.grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Combobox(models, textvariable=self.srt_model_var, values=MODELS, width=26).pack(side="left")
        ttk.Label(models, text="Model title/desc/thumbnail").pack(side="left", padx=(16, 6))
        ttk.Combobox(models, textvariable=self.meta_model_var, values=MODELS, width=26).pack(side="left")
        ttk.Label(models, text="Ngôn ngữ").pack(side="left", padx=(16, 6))
        ttk.Combobox(models, textvariable=self.language_var, values=LANGUAGES, width=24).pack(side="left")

        folder = ttk.Frame(main)
        folder.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        folder.columnconfigure(1, weight=1)
        ttk.Label(folder, text="Thư mục lưu").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(folder, textvariable=self.output_dir_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(folder, text="Chọn...", command=self._choose_output_dir).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Label(folder, text="(để trống = lưu cạnh từng video)").grid(row=0, column=3, padx=(8, 0))
        ttk.Checkbutton(folder, text="Ghi đè SRT đã có", variable=self.overwrite_srt_var).grid(
            row=0, column=4, padx=(16, 0)
        )

        instr = ttk.LabelFrame(
            main,
            text="Yêu cầu thêm cho title/description (phong cách kênh, từ khóa, link...)",
            padding=4,
        )
        instr.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.extra_text = scrolledtext.ScrolledText(instr, wrap="word", height=3)
        self.extra_text.pack(fill="x")

        list_frame = ttk.Frame(main)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            list_frame, columns=("video", "srt", "meta"), show="headings", selectmode="extended", height=6
        )
        for column, title, width in (
            ("video", "Video", 560),
            ("srt", "SRT", 220),
            ("meta", "Title/Desc/Thumbnail", 220),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == "video")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=6)
        ttk.Button(actions, text="Thêm video...", command=self._add_videos).pack(side="left")
        ttk.Button(actions, text="Xóa đã chọn", command=self._remove_selected).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="Xóa hết", command=self._clear).pack(side="left", padx=(6, 0))
        ttk.Separator(actions, orient="vertical").pack(side="left", fill="y", padx=10)
        self.srt_button = ttk.Button(actions, text="Lấy SRT", command=lambda: self._start((TASK_SRT,)))
        self.srt_button.pack(side="left")
        self.meta_button = ttk.Button(
            actions, text="Tạo title/desc/thumbnail", command=lambda: self._start((TASK_META,))
        )
        self.meta_button.pack(side="left", padx=(6, 0))
        self.both_button = ttk.Button(
            actions,
            text="Xếp queue: SRT + Title/Desc",
            command=lambda: self._start((TASK_SRT, TASK_META)),
        )
        self.both_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(actions, text="Dừng", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Label(actions, text="(không chọn dòng nào = chạy tất cả video)").pack(
            side="left", padx=(10, 0)
        )

        detail = ttk.LabelFrame(main, text="Kết quả của video đang chọn", padding=6)
        detail.grid(row=5, column=0, sticky="nsew")
        detail.columnconfigure(1, weight=1)
        detail.rowconfigure(1, weight=1)
        detail.rowconfigure(2, weight=1)
        ttk.Label(detail, text="Title").grid(row=0, column=0, sticky="nw", padx=(0, 8))
        ttk.Entry(detail, textvariable=self.title_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(detail, text="Copy", command=lambda: self._copy(self.title_var.get())).grid(
            row=0, column=2, padx=(6, 0)
        )
        ttk.Label(detail, text="Description").grid(
            row=1, column=0, sticky="nw", padx=(0, 8), pady=(6, 0)
        )
        self.desc_text = scrolledtext.ScrolledText(detail, wrap="word", height=6)
        self.desc_text.grid(row=1, column=1, sticky="nsew", pady=(6, 0))
        ttk.Button(detail, text="Copy", command=lambda: self._copy(self._text(self.desc_text))).grid(
            row=1, column=2, sticky="n", padx=(6, 0), pady=(6, 0)
        )
        ttk.Label(detail, text="Thumbnail\nprompt").grid(
            row=2, column=0, sticky="nw", padx=(0, 8), pady=(6, 0)
        )
        self.thumb_text = scrolledtext.ScrolledText(detail, wrap="word", height=5)
        self.thumb_text.grid(row=2, column=1, sticky="nsew", pady=(6, 0))
        ttk.Button(detail, text="Copy", command=lambda: self._copy(self._text(self.thumb_text))).grid(
            row=2, column=2, sticky="n", padx=(6, 0), pady=(6, 0)
        )
        ttk.Label(main, textvariable=self.status_var).grid(row=6, column=0, sticky="w", pady=(6, 0))

    @staticmethod
    def _text(widget: scrolledtext.ScrolledText) -> str:
        return widget.get("1.0", "end-1c").strip()

    # ---------- settings ----------
    def _load_settings(self) -> None:
        data = self.settings.load()
        self.remember_key_var.set(bool(data.get("remember_api_key", False)))
        saved_key = str(data.get("api_key", "")) if self.remember_key_var.get() else ""
        editor_key = str(SettingsStore(EDITOR_SETTINGS_PATH).load().get("api_key", ""))
        self.api_key_var.set(saved_key or os.environ.get("GEMINI_API_KEY", "") or editor_key)
        self.srt_model_var.set(str(data.get("srt_model", "")) or MODELS[0])
        self.meta_model_var.set(str(data.get("meta_model", "")) or MODELS[0])
        self.language_var.set(str(data.get("language", "")) or LANGUAGE_AUTO)
        self.output_dir_var.set(str(data.get("output_dir", "")))
        self.overwrite_srt_var.set(bool(data.get("overwrite_srt", False)))
        self.extra_text.insert("1.0", str(data.get("extra", "")))

    def _save_settings(self) -> None:
        remember = self.remember_key_var.get()
        self.settings.save(
            {
                "srt_model": self.srt_model_var.get().strip(),
                "meta_model": self.meta_model_var.get().strip(),
                "language": self.language_var.get().strip(),
                "output_dir": self.output_dir_var.get().strip(),
                "overwrite_srt": self.overwrite_srt_var.get(),
                "extra": self._text(self.extra_text),
                "remember_api_key": remember,
                "api_key": self.api_key_var.get().strip() if remember else "",
            }
        )

    # ---------- list handling ----------
    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.window, title="Thư mục lưu SRT và metadata")
        if path:
            self.output_dir_var.set(path)

    def _add_videos(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.window, title="Chọn video", filetypes=VIDEO_TYPES
        )
        for raw in paths:
            path = Path(raw).resolve()
            iid = str(path)
            if iid in self.items:
                continue
            self.items[iid] = VideoItem(path=path)
            self.tree.insert("", "end", iid=iid, values=(str(path), "-", "-"))
            self._load_existing(iid)

    def _load_existing(self, iid: str) -> None:
        """Pick up SRT / metadata files from a previous run."""
        item = self.items[iid]
        out_dir = self._out_dir(item.path)
        srt = out_dir / f"{item.path.stem}.srt"
        if srt.is_file():
            item.srt_path = srt
            item.srt_status = "Đã có file SRT"
        meta_json = out_dir / f"{item.path.stem}_meta.json"
        if meta_json.is_file():
            try:
                data = json.loads(meta_json.read_text(encoding="utf-8"))
                item.meta = VideoMetadata(
                    str(data["title"]),
                    str(data["description"]),
                    str(data["thumbnail_prompt"]),
                    [str(tag) for tag in data.get("tags", [])],
                )
                item.meta_status = "Đã có kết quả"
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self._refresh_row(iid)

    def _refresh_row(self, iid: str) -> None:
        if self.tree.exists(iid):
            item = self.items[iid]
            self.tree.item(iid, values=(str(item.path), item.srt_status, item.meta_status))

    def _busy(self) -> bool:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Đang chạy", "Dừng queue trước khi xóa video.", parent=self.window)
            return True
        return False

    def _remove_selected(self) -> None:
        if self._busy():
            return
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.tree.delete(iid)

    def _clear(self) -> None:
        if self._busy():
            return
        self.items.clear()
        self.tree.delete(*self.tree.get_children())

    def _show_selected(self, _event: object = None) -> None:
        selection = self.tree.selection()
        meta = self.items[selection[0]].meta if selection and selection[0] in self.items else None
        self.title_var.set(meta.title if meta else "")
        for widget, value in (
            (self.desc_text, meta.description if meta else ""),
            (self.thumb_text, meta.thumbnail_prompt if meta else ""),
        ):
            widget.delete("1.0", "end")
            widget.insert("1.0", value)

    def _copy(self, value: str) -> None:
        if not value.strip():
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(value.strip())
        self.window.update()
        self.status_var.set("Đã copy.")

    def _out_dir(self, video: Path) -> Path:
        raw = self.output_dir_var.get().strip()
        return Path(raw) if raw else video.parent

    # ---------- running ----------
    def _start(self, tasks: tuple[str, ...]) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            api_key = self.api_key_var.get().strip()
            if not api_key:
                raise ValueError("Nhập Gemini API key.")
            srt_model = self.srt_model_var.get().strip()
            meta_model = self.meta_model_var.get().strip()
            if not srt_model or not meta_model:
                raise ValueError("Chọn model cho cả hai việc.")
            ids = list(self.tree.selection()) or list(self.tree.get_children())
            if not ids:
                raise ValueError("Thêm video trước.")
            ensure_ffmpeg()
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Chưa chạy được", str(exc), parent=self.window)
            return
        config = {
            "api_key": api_key,
            "srt_model": srt_model,
            "meta_model": meta_model,
            "language": self.language_var.get().strip() or LANGUAGE_AUTO,
            "extra": self._text(self.extra_text),
            "overwrite_srt": self.overwrite_srt_var.get(),
            "output_dir": self.output_dir_var.get().strip(),
        }
        self.cancel_event.clear()
        self._set_busy(True)
        self.worker = threading.Thread(
            target=self._run_queue, args=(ids, tasks, config), daemon=True
        )
        self.worker.start()

    def _run_queue(self, ids: list[str], tasks: tuple[str, ...], config: dict[str, Any]) -> None:
        failed = 0
        try:
            for position, iid in enumerate(ids, start=1):
                if self.cancel_event.is_set():
                    raise InterruptedError("Stopped by user.")
                item = self.items[iid]
                self._log(f"[{position}/{len(ids)}] {item.path.name}")
                try:
                    self._process(iid, item, tasks, config)
                except InterruptedError:
                    raise
                except Exception as exc:
                    failed += 1
                    traceback.print_exc()
                    failed_task = TASK_META if TASK_META in tasks else TASK_SRT
                    self.events.put(("row", (iid, failed_task, f"Lỗi: {exc}")))
            self.events.put(("done", failed))
        except InterruptedError:
            self.events.put(("stopped", None))
        except Exception as exc:
            traceback.print_exc()
            self.events.put(("error", str(exc)))

    def _process(
        self, iid: str, item: VideoItem, tasks: tuple[str, ...], config: dict[str, Any]
    ) -> None:
        video = item.path
        out_dir = Path(config["output_dir"]) if config["output_dir"] else video.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        srt_path = out_dir / f"{video.stem}.srt"
        srt_exists = srt_path.is_file()
        # Metadata needs a transcript, so create one when it is missing.
        want_srt = TASK_SRT in tasks and (config["overwrite_srt"] or not srt_exists)
        need_srt = want_srt or (TASK_META in tasks and not srt_exists)
        if TASK_SRT in tasks and not want_srt:
            self.events.put(("srt", (iid, srt_path, "Bỏ qua: đã có SRT")))
        if need_srt:
            self.events.put(("row", (iid, TASK_SRT, "Đang lấy SRT...")))
            info = probe_media(video)
            if info.duration <= 0:
                raise RuntimeError("Không đọc được độ dài video.")
            if not info.has_audio:
                raise RuntimeError("Video không có audio.")
            client = GeminiJsonClient(
                config["api_key"], config["srt_model"], self._log, self.cancel_event
            )
            cues = transcribe_media(
                client, video, info.duration, out_dir, self._log, self.cancel_event
            )
            write_srt(cues, srt_path)
            self.events.put(("srt", (iid, srt_path, f"Xong: {len(cues)} cue")))
        if TASK_META not in tasks:
            return
        cues, _, _ = read_srt(srt_path)
        self.events.put(("row", (iid, TASK_META, "Đang tạo...")))
        client = GeminiJsonClient(
            config["api_key"], config["meta_model"], self._log, self.cancel_event
        )
        meta = generate_video_metadata(
            client, video.name, cues, config["language"], config["extra"]
        )
        base = out_dir / f"{video.stem}_meta"
        base.with_suffix(".json").write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        base.with_suffix(".txt").write_text(meta.to_text(), encoding="utf-8")
        self.events.put(("meta", (iid, meta)))

    def _log(self, message: str) -> None:
        self.events.put(("status", message))

    def _flush_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "row":
                    iid, task, text = payload
                    if iid in self.items:
                        item = self.items[iid]
                        if task == TASK_SRT:
                            item.srt_status = text
                        else:
                            item.meta_status = text
                        self._refresh_row(iid)
                elif kind == "srt":
                    iid, path, text = payload
                    if iid in self.items:
                        self.items[iid].srt_path = path
                        self.items[iid].srt_status = text
                        self._refresh_row(iid)
                elif kind == "meta":
                    iid, meta = payload
                    if iid in self.items:
                        self.items[iid].meta = meta
                        self.items[iid].meta_status = "Xong"
                        self._refresh_row(iid)
                        if self.tree.selection() == (iid,):
                            self._show_selected()
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
        state = "disabled" if busy else "normal"
        for button in (self.srt_button, self.meta_button, self.both_button):
            button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _stop(self) -> None:
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("Đang dừng sau request Gemini hiện tại...")

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
    VideoMetaWindow(root)
    root.mainloop()
