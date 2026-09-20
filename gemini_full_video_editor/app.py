from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .asset_scanner import scan_assets
from .edit_planner import EditPlanner
from .gemini_client import GeminiJsonClient
from .media_utils import ensure_ffmpeg, probe_media
from .models import Asset, Cue, EditPlan, MediaInfo, RunConfig
from .plan_validator import (
    PlanValidator,
    format_validation_report,
    load_plan,
    save_plan,
)
from .renderer import FFmpegRenderer
from .semantic_aligner import SemanticAligner
from .settings import SettingsStore
from .srt_utils import format_timestamp, read_srt, read_text_file, write_srt


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PACKAGE_ROOT / "gemini_ai_full_video_editor.settings.json"
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "full_video_outputs"
DEFAULT_MODELS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)
RESOLUTIONS = ("1920x1080", "1280x720", "1080x1920")


def build_run_directory(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = root / datetime.now().strftime("edit_%Y%m%d_%H%M%S")
    candidate = base
    number = 2
    while candidate.exists():
        candidate = Path(f"{base}_{number}")
        number += 1
    candidate.mkdir(parents=True)
    (candidate / "temp").mkdir()
    return candidate


class FullVideoEditorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Gemini AI Full Video Editor")
        self.root.geometry("1120x900")
        self.root.minsize(900, 720)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.log_lock = threading.Lock()
        self.active_log_path: Path | None = None
        self.settings = SettingsStore(SETTINGS_PATH)

        self.api_key_var = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        self.model_var = tk.StringVar(value=DEFAULT_MODELS[0])
        self.remember_key_var = tk.BooleanVar(value=False)
        self.source_video_var = tk.StringVar()
        self.source_srt_var = tk.StringVar()
        self.original_script_var = tk.StringVar()
        self.target_voice_var = tk.StringVar()
        self.target_srt_var = tk.StringVar()
        self.assets_folder_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.plan_path_var = tk.StringVar()
        self.resolution_var = tk.StringVar(value="1920x1080")
        self.fps_var = tk.StringVar(value="30")
        self.burn_subtitles_var = tk.BooleanVar(value=False)
        self.export_srt_var = tk.BooleanVar(value=False)
        self.subtitle_font_size_var = tk.StringVar(value="14")
        self.original_audio_mode_var = tk.StringVar(value="mute")
        self.original_audio_volume_var = tk.StringVar(value="0.10")
        self.normalize_audio_var = tk.BooleanVar(value=False)
        self.max_speed_up_var = tk.StringVar(value="1.20")
        self.max_slow_down_var = tk.StringVar(value="0.85")
        self.status_var = tk.StringVar(value="Ready")
        self.last_output_var = tk.StringVar()

        self._build_ui()
        self._load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._flush_events)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(6, weight=1)

        gemini = ttk.LabelFrame(main, text="Gemini", padding=8)
        gemini.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        gemini.columnconfigure(1, weight=1)
        ttk.Label(gemini, text="API Key").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(gemini, textvariable=self.api_key_var, show="*").grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Checkbutton(
            gemini, text="Remember API key", variable=self.remember_key_var
        ).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(gemini, text="Model").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Combobox(
            gemini,
            textvariable=self.model_var,
            values=DEFAULT_MODELS,
            state="normal",
        ).grid(row=1, column=1, sticky="ew", pady=(7, 0))
        ttk.Label(gemini, text="Select or type a model ID").grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(7, 0)
        )

        inputs = ttk.LabelFrame(main, text="Inputs and project files", padding=8)
        inputs.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        inputs.columnconfigure(1, weight=1)
        self._path_row(inputs, 0, "Original Video", self.source_video_var, self._choose_source_video)
        self._path_row(inputs, 1, "Original Video SRT", self.source_srt_var, self._choose_source_srt)
        self._path_row(inputs, 2, "Original Script TXT", self.original_script_var, self._choose_script)
        self._path_row(inputs, 3, "Target Voice MP3", self.target_voice_var, self._choose_target_voice)
        self._path_row(inputs, 4, "Target Voice SRT", self.target_srt_var, self._choose_target_srt)
        self._path_row(inputs, 5, "Assets Folder (optional)", self.assets_folder_var, self._choose_assets)
        self._path_row(inputs, 6, "Output Folder", self.output_dir_var, self._choose_output)
        self._path_row(inputs, 7, "Edit Plan JSON", self.plan_path_var, self._choose_plan)

        options = ttk.LabelFrame(main, text="Editing options", padding=8)
        options.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(options, text="Resolution").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.resolution_var,
            values=RESOLUTIONS,
            state="normal",
            width=14,
        ).grid(row=0, column=1, sticky="w", padx=(6, 20))
        ttk.Label(options, text="FPS").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(options, from_=15, to=60, width=6, textvariable=self.fps_var).grid(
            row=0, column=3, sticky="w", padx=(6, 20)
        )
        ttk.Checkbutton(
            options,
            text="Render subtitles onto video",
            variable=self.burn_subtitles_var,
        ).grid(row=0, column=4, sticky="w", padx=(0, 16))
        ttk.Checkbutton(
            options,
            text="Export sidecar SRT",
            variable=self.export_srt_var,
        ).grid(row=0, column=5, sticky="w")

        ttk.Label(options, text="Original audio").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Radiobutton(
            options, text="Mute", value="mute", variable=self.original_audio_mode_var
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Radiobutton(
            options,
            text="Background",
            value="background",
            variable=self.original_audio_mode_var,
        ).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(options, text="Background volume").grid(
            row=1, column=3, sticky="e", pady=(8, 0)
        )
        ttk.Entry(options, width=7, textvariable=self.original_audio_volume_var).grid(
            row=1, column=4, sticky="w", pady=(8, 0), padx=(6, 16)
        )
        ttk.Checkbutton(
            options, text="Normalize final audio", variable=self.normalize_audio_var
        ).grid(row=1, column=5, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Max Speed Up").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, width=7, textvariable=self.max_speed_up_var).grid(
            row=2, column=1, sticky="w", padx=(6, 20), pady=(8, 0)
        )
        ttk.Label(options, text="Max Slow Down").grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(options, width=7, textvariable=self.max_slow_down_var).grid(
            row=2, column=3, sticky="w", padx=(6, 20), pady=(8, 0)
        )
        ttk.Label(options, text="Subtitle font size").grid(
            row=2, column=4, sticky="e", pady=(8, 0)
        )
        ttk.Spinbox(
            options,
            from_=8,
            to=30,
            width=6,
            textvariable=self.subtitle_font_size_var,
        ).grid(row=2, column=5, sticky="w", padx=(6, 0), pady=(8, 0))

        instructions = ttk.LabelFrame(main, text="Additional Gemini editing instructions", padding=6)
        instructions.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        instructions.columnconfigure(0, weight=1)
        self.instructions_text = scrolledtext.ScrolledText(
            instructions, height=4, wrap="word"
        )
        self.instructions_text.grid(row=0, column=0, sticky="ew")

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self.analyze_button = ttk.Button(
            actions, text="Analyze & Create Plan", command=lambda: self._start("analyze")
        )
        self.analyze_button.pack(side="left")
        self.render_button = ttk.Button(
            actions, text="Render Video", command=lambda: self._start("render")
        )
        self.render_button.pack(side="left", padx=(8, 0))
        self.all_button = ttk.Button(
            actions, text="Analyze + Render", command=lambda: self._start("all")
        )
        self.all_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(
            actions, text="Stop", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open Output Folder", command=self._open_output).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        recent = ttk.Frame(main)
        recent.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        recent.columnconfigure(1, weight=1)
        ttk.Label(recent, text="Latest output").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(recent, textvariable=self.last_output_var, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )

        logs = ttk.LabelFrame(main, text="Realtime log", padding=6)
        logs.grid(row=6, column=0, sticky="nsew")
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(logs, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

    @staticmethod
    def _path_row(
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        ttk.Button(parent, text="Choose...", command=command).grid(
            row=row, column=2, padx=(8, 0), pady=3
        )

    def _choose_file(self, title: str, filetypes: tuple[tuple[str, str], ...]) -> str:
        return filedialog.askopenfilename(title=title, filetypes=filetypes)

    def _choose_source_video(self) -> None:
        path = self._choose_file(
            "Choose original video",
            (("Video", "*.mp4 *.mov *.mkv *.webm"), ("All files", "*.*")),
        )
        if path:
            self.source_video_var.set(path)

    def _choose_source_srt(self) -> None:
        path = self._choose_file("Choose original SRT", (("SubRip", "*.srt"),))
        if path:
            self.source_srt_var.set(path)

    def _choose_script(self) -> None:
        path = self._choose_file("Choose original script", (("Text", "*.txt"), ("All files", "*.*")))
        if path:
            self.original_script_var.set(path)

    def _choose_target_voice(self) -> None:
        path = self._choose_file(
            "Choose target voice", (("Audio", "*.mp3 *.wav *.m4a *.aac"), ("All files", "*.*"))
        )
        if path:
            self.target_voice_var.set(path)

    def _choose_target_srt(self) -> None:
        path = self._choose_file("Choose target SRT", (("SubRip", "*.srt"),))
        if path:
            self.target_srt_var.set(path)

    def _choose_assets(self) -> None:
        path = filedialog.askdirectory(title="Choose optional assets folder")
        if path:
            self.assets_folder_var.set(path)

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.output_dir_var.set(path)

    def _choose_plan(self) -> None:
        path = self._choose_file("Choose edit plan", (("JSON", "*.json"),))
        if path:
            self.plan_path_var.set(path)

    def _load_settings(self) -> None:
        data = self.settings.load()
        mappings = (
            (self.model_var, data.get("model", self.model_var.get())),
            (self.source_video_var, data.get("source_video", "")),
            (self.source_srt_var, data.get("source_srt", "")),
            (self.original_script_var, data.get("original_script", "")),
            (self.target_voice_var, data.get("target_voice", "")),
            (self.target_srt_var, data.get("target_srt", "")),
            (self.assets_folder_var, data.get("assets_folder", "")),
            (self.output_dir_var, data.get("output_dir", str(DEFAULT_OUTPUT_DIR))),
            (self.plan_path_var, data.get("plan_path", "")),
            (self.resolution_var, data.get("resolution", "1920x1080")),
            (self.fps_var, data.get("fps", "30")),
            (self.original_audio_mode_var, data.get("original_audio_mode", "mute")),
            (
                self.original_audio_volume_var,
                data.get("original_audio_volume", "0.10"),
            ),
            (self.max_speed_up_var, data.get("max_speed_up", "1.20")),
            (self.max_slow_down_var, data.get("max_slow_down", "0.85")),
            (self.subtitle_font_size_var, data.get("subtitle_font_size", "14")),
        )
        for variable, value in mappings:
            variable.set(str(value))
        self.burn_subtitles_var.set(bool(data.get("burn_subtitles", False)))
        self.export_srt_var.set(bool(data.get("export_srt", False)))
        self.normalize_audio_var.set(bool(data.get("normalize_audio", False)))
        self.remember_key_var.set(bool(data.get("remember_api_key", False)))
        if self.remember_key_var.get():
            self.api_key_var.set(str(data.get("api_key", self.api_key_var.get())))
        self.instructions_text.insert("1.0", str(data.get("extra_instructions", "")))

    def _save_settings(self) -> None:
        remember = self.remember_key_var.get()
        self.settings.save(
            {
                "model": self.model_var.get().strip(),
                "source_video": self.source_video_var.get().strip(),
                "source_srt": self.source_srt_var.get().strip(),
                "original_script": self.original_script_var.get().strip(),
                "target_voice": self.target_voice_var.get().strip(),
                "target_srt": self.target_srt_var.get().strip(),
                "assets_folder": self.assets_folder_var.get().strip(),
                "output_dir": self.output_dir_var.get().strip(),
                "plan_path": self.plan_path_var.get().strip(),
                "resolution": self.resolution_var.get().strip(),
                "fps": self.fps_var.get().strip(),
                "burn_subtitles": self.burn_subtitles_var.get(),
                "export_srt": self.export_srt_var.get(),
                "original_audio_mode": self.original_audio_mode_var.get(),
                "original_audio_volume": self.original_audio_volume_var.get().strip(),
                "normalize_audio": self.normalize_audio_var.get(),
                "max_speed_up": self.max_speed_up_var.get().strip(),
                "max_slow_down": self.max_slow_down_var.get().strip(),
                "subtitle_font_size": self.subtitle_font_size_var.get().strip(),
                "extra_instructions": self.instructions_text.get("1.0", "end").strip(),
                "remember_api_key": remember,
                "api_key": self.api_key_var.get().strip() if remember else "",
            }
        )

    @staticmethod
    def _float_field(value: str, label: str, minimum: float, maximum: float) -> float:
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label} must be a number.") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}.")
        return number

    def _collect_config(self, require_api_key: bool) -> RunConfig:
        def required_file(raw: str, label: str) -> Path:
            path = Path(raw.strip())
            if not path.is_file():
                raise ValueError(f"{label} does not exist.")
            return path.resolve()

        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if require_api_key and not api_key:
            raise ValueError("Enter a Gemini API key.")
        if require_api_key and not model:
            raise ValueError("Select or enter a Gemini model ID.")
        output_text = self.output_dir_var.get().strip()
        if not output_text:
            raise ValueError("Choose an output folder.")
        assets_text = self.assets_folder_var.get().strip()
        assets_folder = Path(assets_text).resolve() if assets_text else None
        if assets_folder is not None and not assets_folder.is_dir():
            raise ValueError("Assets folder does not exist.")
        try:
            fps = int(self.fps_var.get().strip())
        except ValueError as exc:
            raise ValueError("FPS must be an integer.") from exc
        if not 15 <= fps <= 60:
            raise ValueError("FPS must be between 15 and 60.")
        try:
            subtitle_font_size = int(self.subtitle_font_size_var.get().strip())
        except ValueError as exc:
            raise ValueError("Subtitle font size must be an integer.") from exc
        if not 8 <= subtitle_font_size <= 30:
            raise ValueError("Subtitle font size must be between 8 and 30.")
        config = RunConfig(
            api_key=api_key,
            model=model,
            source_video=required_file(self.source_video_var.get(), "Original video"),
            source_srt=required_file(self.source_srt_var.get(), "Original SRT"),
            original_script=required_file(self.original_script_var.get(), "Original script"),
            target_voice=required_file(self.target_voice_var.get(), "Target voice"),
            target_srt=required_file(self.target_srt_var.get(), "Target SRT"),
            assets_folder=assets_folder,
            output_dir=Path(output_text).resolve(),
            resolution=self.resolution_var.get().strip(),
            fps=fps,
            burn_subtitles=self.burn_subtitles_var.get(),
            export_srt=self.export_srt_var.get(),
            original_audio_mode=self.original_audio_mode_var.get(),
            original_audio_volume=self._float_field(
                self.original_audio_volume_var.get(), "Background volume", 0.0, 0.3
            ),
            normalize_audio=self.normalize_audio_var.get(),
            max_speed_up=self._float_field(
                self.max_speed_up_var.get(), "Max Speed Up", 1.0, 2.0
            ),
            max_slow_down=self._float_field(
                self.max_slow_down_var.get(), "Max Slow Down", 0.5, 1.0
            ),
            extra_instructions=self.instructions_text.get("1.0", "end").strip(),
            subtitle_font_size=subtitle_font_size,
        )
        config.output_size
        if config.original_audio_mode not in {"mute", "background"}:
            raise ValueError("Original audio mode must be Mute or Background.")
        return config

    def _start(self, mode: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._collect_config(require_api_key=mode in {"analyze", "all"})
            plan_path = Path(self.plan_path_var.get().strip()).resolve() if mode == "render" else None
            if mode == "render" and (plan_path is None or not plan_path.is_file()):
                raise ValueError("Choose an existing edit_plan.json before rendering.")
            ensure_ffmpeg()
            self._save_settings()
        except Exception as exc:
            messagebox.showerror("Cannot start", str(exc))
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("Running...")
        self.worker = threading.Thread(
            target=self._run_worker, args=(mode, config, plan_path), daemon=True
        )
        self.worker.start()

    def _run_worker(self, mode: str, config: RunConfig, plan_path: Path | None) -> None:
        run_dir: Path | None = None
        try:
            if mode in {"analyze", "all"}:
                run_dir = build_run_directory(config.output_dir)
                self.active_log_path = run_dir / "logs.txt"
                self._log(f"Run directory: {run_dir}")
                plan_path, report = self._analyze(config, run_dir)
                self.events.put(("plan", (str(plan_path), format_validation_report(report))))
            if mode in {"render", "all"}:
                if plan_path is None:
                    raise RuntimeError("No edit plan is available for rendering.")
                run_dir = plan_path.parent
                self.active_log_path = run_dir / "logs.txt"
                output = self._render(config, plan_path, run_dir)
                self.events.put(("complete", (str(run_dir), str(output))))
            else:
                self.events.put(("analysis_complete", str(run_dir)))
        except InterruptedError as exc:
            self.events.put(("cancelled", str(exc)))
        except Exception as exc:
            self._log(traceback.format_exc())
            self.events.put(("error", (str(exc), str(run_dir) if run_dir else "")))

    def _prepare_inputs(
        self, config: RunConfig
    ) -> tuple[list[Cue], list[Cue], str, MediaInfo, MediaInfo, list[Asset]]:
        self._log("Reading source SRT...")
        source_cues, source_encoding, source_repaired = read_srt(config.source_srt)
        self._log("Reading target SRT...")
        target_cues, target_encoding, target_repaired = read_srt(config.target_srt)
        if not source_cues:
            raise RuntimeError("Original SRT contains no valid cues.")
        if not target_cues:
            raise RuntimeError("Target SRT contains no valid cues.")
        script, script_encoding, script_repaired = read_text_file(config.original_script)
        if not script.strip():
            raise RuntimeError("Original script is empty.")
        source_info = probe_media(config.source_video)
        target_info = probe_media(config.target_voice)
        if source_info.duration <= 0:
            raise RuntimeError("Original video duration could not be determined.")
        if target_info.duration <= 0:
            raise RuntimeError("Target voice duration could not be determined.")
        source_cues = [
            replace(cue, end=min(cue.end, source_info.duration))
            for cue in source_cues
            if cue.start < source_info.duration and min(cue.end, source_info.duration) > cue.start
        ]
        target_cues = [
            replace(cue, end=min(cue.end, target_info.duration))
            for cue in target_cues
            if cue.start < target_info.duration and min(cue.end, target_info.duration) > cue.start
        ]
        if not source_cues or not target_cues:
            raise RuntimeError("SRT timestamps fall outside their corresponding media duration.")
        self._log(
            f"Source video: {format_timestamp(source_info.duration)}; "
            f"source cues: {len(source_cues)} ({source_encoding})."
        )
        self._log(
            f"Target voice: {format_timestamp(target_info.duration)}; "
            f"target cues: {len(target_cues)} ({target_encoding})."
        )
        repaired_names = []
        if source_repaired:
            repaired_names.append("source SRT")
        if target_repaired:
            repaired_names.append("target SRT")
        if script_repaired:
            repaired_names.append("original script")
        if repaired_names:
            self._log("Repaired encoding/mojibake in: " + ", ".join(repaired_names) + ".")
        self._log(f"Original script encoding: {script_encoding}.")
        assets = scan_assets(config.assets_folder, self._log)
        self._log(f"Assets available: {len(assets)}.")
        return source_cues, target_cues, script, source_info, target_info, assets

    def _analyze(self, config: RunConfig, run_dir: Path) -> tuple[Path, dict[str, Any]]:
        source_cues, target_cues, script, source_info, target_info, assets = self._prepare_inputs(config)
        (run_dir / "asset_manifest.json").write_text(
            json.dumps([asset.to_dict() for asset in assets], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        client = GeminiJsonClient(
            config.api_key, config.model, self._log, self.cancel_event
        )
        if len(script) > 40_000:
            self._log("Original script is long; creating a compact full-script context first...")
            script_context = client.summarize_script(script)
        else:
            script_context = script
        (run_dir / "script_context.txt").write_text(script_context, encoding="utf-8")

        self._log("Creating semantic alignment...")
        aligner = SemanticAligner(client, self._log)
        alignments, alignment_report = aligner.align(
            script_context, source_cues, target_cues, target_info.duration
        )
        alignment_payload = {
            "version": 1,
            "sourceCueCount": len(source_cues),
            "targetCueCount": len(target_cues),
            "alignments": [item.to_dict() for item in alignments],
            "validation": alignment_report,
        }
        (run_dir / "semantic_alignment.json").write_text(
            json.dumps(alignment_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._log(
            f"Alignment: {alignment_report['matchedTargetCueCount']}/"
            f"{alignment_report['targetCueCount']} target cues matched."
        )

        self._log("Creating edit plan...")
        project = config.project_dict()
        project["sourceMetadata"] = source_info.to_dict()
        project["targetVoiceDuration"] = round(target_info.duration, 3)
        project["assets"] = [asset.to_dict() for asset in assets]
        planner = EditPlanner(client, self._log)
        raw_plan = planner.create_plan(
            script_context,
            source_cues,
            target_cues,
            alignments,
            source_info,
            target_info.duration,
            assets,
            config.extra_instructions,
            project,
        )
        edit_plan_path = run_dir / "edit_plan.json"
        save_plan(raw_plan, edit_plan_path)
        self._log(f"Generated {len(raw_plan.segments)} raw edit segments.")

        self._log("Validating edit plan...")
        validator = PlanValidator(self._log)
        validated, report = validator.validate(
            raw_plan,
            source_info,
            target_info.duration,
            source_cues,
            target_cues,
            assets,
            config.max_speed_up,
            config.max_slow_down,
        )
        save_plan(validated, run_dir / "validated_edit_plan.json")
        (run_dir / "validation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._log("Validation summary:\n" + format_validation_report(report))
        return edit_plan_path, report

    def _render(self, config: RunConfig, plan_path: Path, run_dir: Path) -> Path:
        source_cues, target_cues, _, source_info, target_info, assets = self._prepare_inputs(config)
        self._log(f"Loading editable plan: {plan_path}")
        raw_plan = load_plan(plan_path)
        validator = PlanValidator(self._log)
        validated, report = validator.validate(
            raw_plan,
            source_info,
            target_info.duration,
            source_cues,
            target_cues,
            assets,
            config.max_speed_up,
            config.max_slow_down,
        )
        save_plan(validated, run_dir / "validated_edit_plan.json")
        (run_dir / "validation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._log("Render-time validation summary:\n" + format_validation_report(report))
        renderer = FFmpegRenderer(
            config, source_info, assets, self._log, self.cancel_event
        )
        if config.burn_subtitles:
            self._log(
                f"Subtitles will be rendered onto the video "
                f"(font size {config.subtitle_font_size})."
            )
        else:
            self._log("Subtitles will NOT be rendered onto the video.")
        self._log(
            "Sidecar SRT export is " + ("enabled." if config.export_srt else "disabled.")
        )
        normalized_target_srt = run_dir / "temp" / "target_voice_normalized.srt"
        normalized_target_srt.parent.mkdir(parents=True, exist_ok=True)
        write_srt(target_cues, normalized_target_srt)
        return renderer.render(validated, run_dir, normalized_target_srt)

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self.events.put(("log", line))
        path = self.active_log_path
        if path is not None:
            try:
                with self.log_lock:
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
            except OSError:
                pass

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _flush_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "plan":
                    plan_path, report = payload
                    self.plan_path_var.set(plan_path)
                    self.last_output_var.set(str(Path(plan_path).parent))
                    self._append_log(report)
                elif kind == "analysis_complete":
                    self.status_var.set("Plan ready")
                    self.last_output_var.set(str(payload))
                    self._set_busy(False)
                    messagebox.showinfo(
                        "Plan ready",
                        "Planning is complete. Inspect/edit edit_plan.json, then click Render Video.",
                    )
                elif kind == "complete":
                    run_dir, output = payload
                    self.status_var.set("Complete")
                    self.last_output_var.set(run_dir)
                    self._set_busy(False)
                    messagebox.showinfo("Complete", f"Final video created:\n{output}")
                elif kind == "cancelled":
                    self.status_var.set("Stopped")
                    self._set_busy(False)
                    self._append_log(str(payload))
                elif kind == "error":
                    message, partial_dir = payload
                    if partial_dir:
                        self.last_output_var.set(partial_dir)
                    self.status_var.set("Error")
                    self._set_busy(False)
                    messagebox.showerror("Operation failed", message)
        except queue.Empty:
            pass
        self.root.after(120, self._flush_events)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.analyze_button.configure(state=state)
        self.render_button.configure(state=state)
        self.all_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("Stopping...")
            self._log("Stop requested; the current Gemini retry or FFmpeg process will stop safely.")

    def _open_output(self) -> None:
        raw = self.last_output_var.get().strip() or self.output_dir_var.get().strip()
        path = Path(raw)
        if not path.exists():
            messagebox.showinfo("No output", "The output folder does not exist yet.")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Cannot open folder", str(exc))

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Operation running", "Stop the task and close the app?"):
                return
            self.cancel_event.set()
        try:
            self._save_settings()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    FullVideoEditorApp(root)
    root.mainloop()
