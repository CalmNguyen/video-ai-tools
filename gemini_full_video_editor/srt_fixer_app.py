from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .gemini_client import GeminiJsonClient
from .settings import DEFAULT_MODELS, FLASH_MODELS, SettingsStore
from .srt_fixer import (
    SrtBlock,
    SrtChange,
    fix_srt,
    format_srt_blocks,
    parse_srt_blocks,
    result_blocks,
    split_srt_and_notes,
)
from .srt_utils import read_text_file


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / "gemini_srt_fixer.settings.json"
EDITOR_SETTINGS_PATH = PACKAGE_ROOT / "gemini_ai_full_video_editor.settings.json"
MODELS = FLASH_MODELS + tuple(model for model in DEFAULT_MODELS if model not in FLASH_MODELS)


class SrtFixerWindow:
    """Fix misrecognized words in an SRT using a reference text / context."""

    def __init__(
        self,
        window: tk.Tk | tk.Toplevel,
        srt_text: str = "",
        srt_path: Path | None = None,
        api_key: str = "",
        model: str = "",
        context: str = "",
        on_saved: Callable[[Path, str], None] | None = None,
    ):
        self.window = window
        self.on_saved = on_saved
        self.window.title("Sửa SRT bằng Gemini")
        self.window.geometry("1180x760")
        self.window.minsize(860, 600)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closed = False
        self.changes: list[SrtChange] = []
        self.renumber = False
        self.generated_text = ""
        self.settings = SettingsStore(SETTINGS_PATH)

        self.api_key_var = tk.StringVar()
        self.remember_key_var = tk.BooleanVar(value=False)
        self.model_var = tk.StringVar(value=MODELS[0])
        self.srt_path_var = tk.StringVar(value=str(srt_path) if srt_path else "")
        self.smooth_var = tk.BooleanVar(value=False)
        self.remove_fillers_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Dán SRT cần sửa và văn bản mẫu/ngữ cảnh rồi bấm Sửa bằng Gemini.")

        self._build_ui()
        self._load_settings(api_key, model)
        if context:
            self.context_text.delete("1.0", "end")
            self.context_text.insert("1.0", context)
        if srt_text:
            self.input_text.insert("1.0", srt_text)
        elif srt_path:
            self._load_srt_file(srt_path)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.after(120, self._flush_events)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.window, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        main.rowconfigure(4, weight=1)

        gemini = ttk.Frame(main)
        gemini.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        gemini.columnconfigure(1, weight=1)
        ttk.Label(gemini, text="API Key").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(gemini, textvariable=self.api_key_var, show="*").grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(gemini, text="Nhớ API key", variable=self.remember_key_var).grid(
            row=0, column=2, padx=(8, 16)
        )
        ttk.Label(gemini, text="Model").grid(row=0, column=3, sticky="w", padx=(0, 6))
        ttk.Combobox(gemini, textvariable=self.model_var, values=MODELS, width=24).grid(
            row=0, column=4, sticky="w"
        )

        file_row = ttk.Frame(main)
        file_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        file_row.columnconfigure(1, weight=1)
        ttk.Label(file_row, text="File SRT").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(file_row, textvariable=self.srt_path_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(file_row, text="Mở file SRT...", command=self._choose_srt).grid(
            row=0, column=2, padx=(8, 0)
        )

        inputs = ttk.Panedwindow(main, orient="horizontal")
        inputs.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        source = ttk.LabelFrame(inputs, text="SRT cần sửa (dán vào hoặc mở file)", padding=4)
        self.input_text = self._text_box(source)
        context = ttk.LabelFrame(
            inputs,
            text="Văn bản mẫu / ngữ cảnh đúng (script gốc, tên riêng, thuật ngữ, ghi chú...)",
            padding=4,
        )
        self.context_text = self._text_box(context)
        inputs.add(source, weight=1)
        inputs.add(context, weight=1)

        actions = ttk.Frame(main)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        options = ttk.Frame(actions)
        options.pack(side="top", fill="x", pady=(0, 6))
        ttk.Checkbutton(
            options,
            text="Làm mượt câu: ngắt lại block theo câu/cụm tự nhiên, thêm dấu câu (timestamp tự tính lại)",
            variable=self.smooth_var,
        ).pack(side="left")
        ttk.Checkbutton(
            options, text="Bỏ từ đệm / nói lặp (ha, ờ, thì ok thì...)", variable=self.remove_fillers_var
        ).pack(side="left", padx=(16, 0))
        self.fix_button = ttk.Button(actions, text="Sửa bằng Gemini", command=self._start)
        self.fix_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Dừng", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=(12, 0))

        outputs = ttk.Panedwindow(main, orient="horizontal")
        outputs.grid(row=4, column=0, sticky="nsew")
        result = ttk.LabelFrame(outputs, text="SRT đã sửa (sửa tay thêm được)", padding=4)
        self.result_text = self._text_box(result)
        changes = ttk.LabelFrame(outputs, text="Các chỗ Gemini đã sửa (double-click để xem)", padding=4)
        changes.columnconfigure(0, weight=1)
        changes.rowconfigure(0, weight=1)
        self.changes_tree = ttk.Treeview(
            changes, columns=("number", "before", "after", "note"), show="headings", selectmode="extended"
        )
        for column, title, width in (
            ("number", "#", 50),
            ("before", "Trước", 190),
            ("after", "Sau", 190),
            ("note", "Lý do", 130),
        ):
            self.changes_tree.heading(column, text=title)
            self.changes_tree.column(column, width=width, stretch=column != "number")
        self.changes_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(changes, orient="vertical", command=self.changes_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.changes_tree.configure(yscrollcommand=scrollbar.set)
        self.changes_tree.bind("<Double-1>", self._show_selected_change)
        ttk.Button(changes, text="Bỏ sửa các dòng đã chọn", command=self._revert_selected).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        outputs.add(result, weight=3)
        outputs.add(changes, weight=2)

        buttons = ttk.Frame(main)
        buttons.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Copy all", command=self._copy_all).pack(side="left")
        ttk.Button(buttons, text="Lưu đè file SRT", command=self._save).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Lưu thành...", command=self._save_as).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Đóng", command=self._on_close).pack(side="right")

    @staticmethod
    def _text_box(parent: ttk.LabelFrame) -> scrolledtext.ScrolledText:
        text = scrolledtext.ScrolledText(parent, wrap="word", undo=True, width=40, height=10)
        text.pack(fill="both", expand=True)

        def select_all(_event: object = None) -> str:
            text.tag_add("sel", "1.0", "end-1c")
            return "break"

        text.bind("<Control-a>", select_all)
        text.bind("<Control-A>", select_all)
        return text

    def _load_settings(self, api_key: str, model: str) -> None:
        data = self.settings.load()
        self.remember_key_var.set(bool(data.get("remember_api_key", False)))
        saved_key = str(data.get("api_key", "")) if self.remember_key_var.get() else ""
        editor_key = str(SettingsStore(EDITOR_SETTINGS_PATH).load().get("api_key", ""))
        self.api_key_var.set(
            api_key or saved_key or os.environ.get("GEMINI_API_KEY", "") or editor_key
        )
        self.model_var.set(model or str(data.get("model", "")) or MODELS[0])
        self.smooth_var.set(bool(data.get("smooth", False)))
        self.remove_fillers_var.set(bool(data.get("remove_fillers", False)))
        self.context_text.insert("1.0", str(data.get("context", "")))

    def _save_settings(self) -> None:
        remember = self.remember_key_var.get()
        self.settings.save(
            {
                "model": self.model_var.get().strip(),
                "context": self.context_text.get("1.0", "end").strip(),
                "smooth": self.smooth_var.get(),
                "remove_fillers": self.remove_fillers_var.get(),
                "remember_api_key": remember,
                "api_key": self.api_key_var.get().strip() if remember else "",
            }
        )

    def _choose_srt(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Chọn file SRT cần sửa",
            filetypes=(("SubRip", "*.srt"), ("Text", "*.txt"), ("All files", "*.*")),
        )
        if path:
            self._load_srt_file(Path(path))

    def _load_srt_file(self, path: Path) -> None:
        try:
            content, _, _ = read_text_file(path)
        except OSError as exc:
            messagebox.showerror("Không đọc được SRT", str(exc), parent=self.window)
            return
        self.srt_path_var.set(str(path))
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", content)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            api_key = self.api_key_var.get().strip()
            model = self.model_var.get().strip()
            if not api_key:
                raise ValueError("Nhập Gemini API key.")
            if not model:
                raise ValueError("Chọn hoặc nhập model Gemini.")
            blocks, notes = split_srt_and_notes(self.input_text.get("1.0", "end"))
            if not blocks:
                raise ValueError("Dán SRT cần sửa hoặc mở một file SRT.")
            context = self.context_text.get("1.0", "end").strip()
            if notes:
                # Reference text pasted together with the SRT is used as context.
                context = "\n\n".join(part for part in (context, notes) if part)
            options = (self.smooth_var.get(), self.remove_fillers_var.get())
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Chưa sửa được", str(exc), parent=self.window)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set(
            f"Đang gửi {len(blocks)} cue cho Gemini..."
            + (" (phần text không phải SRT được dùng làm ngữ cảnh)" if notes else "")
        )
        self.worker = threading.Thread(
            target=self._run_worker,
            args=(api_key, model, blocks, context, *options),
            daemon=True,
        )
        self.worker.start()

    def _run_worker(
        self,
        api_key: str,
        model: str,
        blocks: list[SrtBlock],
        context: str,
        smooth: bool,
        remove_fillers: bool,
    ) -> None:
        try:
            client = GeminiJsonClient(api_key, model, self._log, self.cancel_event)
            changes = fix_srt(client, blocks, context, self._log, smooth, remove_fillers)
            self.events.put(("done", (changes, smooth)))
        except InterruptedError:
            self.events.put(("stopped", None))
        except Exception as exc:
            traceback.print_exc()
            self.events.put(("error", str(exc)))

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
                elif kind == "done":
                    changes, smooth = payload
                    self._show_result(changes, smooth)
                    self._set_busy(False)
                    changed = sum(change.changed for change in changes)
                    unit = "nhóm câu" if smooth else "cue"
                    self.status_var.set(
                        f"Xong: Gemini sửa {changed} {unit}, SRT mới có "
                        f"{len(result_blocks(changes))} cue. Kiểm tra danh sách bên phải rồi Copy/Lưu."
                    )
                elif kind == "stopped":
                    self._set_busy(False)
                    self.status_var.set("Đã dừng.")
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set("Lỗi.")
                    messagebox.showerror("Gemini lỗi", str(payload), parent=self.window)
        except queue.Empty:
            pass
        self.window.after(120, self._flush_events)

    def _show_result(self, changes: list[SrtChange], renumber: bool) -> None:
        self.changes = changes
        self.renumber = renumber
        self._render_result()
        self.changes_tree.delete(*self.changes_tree.get_children())
        for index, change in enumerate(changes):
            if not change.changed:
                continue
            self.changes_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    change.label,
                    " | ".join(block.text.replace("\n", " / ") for block in change.before),
                    " | ".join(block.text.replace("\n", " / ") for block in change.after),
                    change.note,
                ),
            )

    def _render_result(self) -> None:
        self.generated_text = format_srt_blocks(result_blocks(self.changes), self.renumber)
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", self.generated_text)

    def _show_selected_change(self, _event: object = None) -> None:
        selection = self.changes_tree.selection()
        if not selection:
            return
        change = self.changes[int(selection[0])]
        first_line = change.after[0].text.split("\n", 1)[0]
        self.result_text.tag_remove("found", "1.0", "end")
        start = self.result_text.search(first_line, "1.0", stopindex="end")
        if not start:
            return
        end = f"{start}+{len(first_line)}c"
        self.result_text.tag_configure("found", background="#fff3a0")
        self.result_text.tag_add("found", start, end)
        self.result_text.see(start)

    def _revert_selected(self) -> None:
        selection = self.changes_tree.selection()
        if not selection:
            messagebox.showinfo("Chưa chọn", "Chọn các dòng muốn bỏ sửa trong danh sách.", parent=self.window)
            return
        edited = self.result_text.get("1.0", "end-1c").strip() != self.generated_text.strip()
        if edited and not messagebox.askyesno(
            "Ghi đè chỗ sửa tay",
            "Bạn đã sửa tay trong ô SRT đã sửa. Bỏ sửa sẽ tạo lại SRT và mất các chỗ sửa tay. Tiếp tục?",
            parent=self.window,
        ):
            return
        for iid in selection:
            change = self.changes[int(iid)]
            change.after = change.before
            change.note = ""
            self.changes_tree.delete(iid)
        self._render_result()
        self.status_var.set(f"Đã bỏ sửa {len(selection)} dòng, giữ nguyên bản gốc ở đó.")

    def _result_or_warn(self) -> str | None:
        value = self.result_text.get("1.0", "end-1c").strip()
        if not value:
            messagebox.showinfo("Chưa có kết quả", "Bấm Sửa bằng Gemini trước.", parent=self.window)
            return None
        return value + "\n"

    def _copy_all(self) -> None:
        value = self._result_or_warn()
        if value is None:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(value)
        self.window.update()
        self.status_var.set(f"Đã copy toàn bộ SRT đã sửa ({len(value)} ký tự).")

    def _save(self) -> None:
        raw = self.srt_path_var.get().strip()
        if not raw:
            self._save_as()
            return
        path = Path(raw)
        if not messagebox.askyesno("Lưu đè", f"Ghi đè file này bằng SRT đã sửa?\n{path}", parent=self.window):
            return
        self._write(path)

    def _save_as(self) -> None:
        raw = self.srt_path_var.get().strip()
        initial = Path(raw) if raw else None
        path = filedialog.asksaveasfilename(
            parent=self.window,
            title="Lưu SRT đã sửa",
            defaultextension=".srt",
            initialdir=str(initial.parent) if initial else None,
            initialfile=f"{initial.stem}_fixed.srt" if initial else "fixed.srt",
            filetypes=(("SubRip", "*.srt"), ("All files", "*.*")),
        )
        if path:
            self._write(Path(path))

    def _write(self, path: Path) -> None:
        value = self._result_or_warn()
        if value is None:
            return
        try:
            parse_srt_blocks(value)
            path.write_text(value, encoding="utf-8-sig")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Không lưu được", str(exc), parent=self.window)
            return
        self.srt_path_var.set(str(path))
        self.status_var.set(f"Đã lưu: {path}")
        if self.on_saved:
            self.on_saved(path, value)

    def _set_busy(self, busy: bool) -> None:
        self.fix_button.configure(state="disabled" if busy else "normal")
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
    SrtFixerWindow(root)
    root.mainloop()
