from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .media_utils import escape_concat_path, escape_filter_path, probe_media
from .models import Asset, Cue, DemoInsert, EditPlan, EditSegment, MediaInfo, RunConfig


BOUNDARY_EPSILON = 0.02


def shift_cues_for_demo_inserts(cues: Sequence[Cue], inserts: Sequence[DemoInsert]) -> list[Cue]:
    """Move narration cues later by the length of every demo played before them."""
    if not inserts:
        return list(cues)

    def shifted(time: float, include_equal: bool) -> float:
        return time + sum(
            item.duration
            for item in inserts
            if item.insert_at < time - BOUNDARY_EPSILON
            or (include_equal and abs(item.insert_at - time) <= BOUNDARY_EPSILON)
        )

    return [
        Cue(cue.id, shifted(cue.start, True), shifted(cue.end, False), cue.text) for cue in cues
    ]


class FFmpegRenderer:
    def __init__(
        self,
        config: RunConfig,
        source_info: MediaInfo,
        assets: Sequence[Asset],
        logger: Callable[[str], None],
        cancel_event: threading.Event,
    ):
        self.config = config
        self.source_info = source_info
        self.assets = {asset.id: asset for asset in assets}
        self.logger = logger
        self.cancel_event = cancel_event
        self.width, self.height = config.output_size
        self.fps = config.fps

    def render(
        self,
        plan: EditPlan,
        run_dir: Path,
        target_srt: Path,
    ) -> Path:
        temp_dir = run_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        inserts = sorted(plan.demo_inserts, key=lambda item: (item.insert_at, item.source_start))
        demo_paths: list[Path] = []
        for index, item in enumerate(inserts, start=1):
            self._check_cancelled()
            self.logger(
                f"Rendering demo insert {index}/{len(inserts)} with original audio "
                f"({item.source_start:.2f}-{item.source_end:.2f}s)..."
            )
            clip_path = temp_dir / f"demo_{index:04d}.mp4"
            self._render_demo_insert(item, clip_path, index)
            demo_paths.append(clip_path)

        timeline_paths: list[Path] = []
        next_insert = 0
        for index, segment in enumerate(plan.segments, start=1):
            self._check_cancelled()
            while (
                next_insert < len(inserts)
                and inserts[next_insert].insert_at <= segment.target_start + BOUNDARY_EPSILON
            ):
                timeline_paths.append(demo_paths[next_insert])
                next_insert += 1
            self.logger(f"Rendering segment {index}/{len(plan.segments)}...")
            clip_path = temp_dir / f"segment_{index:04d}.mp4"
            self._render_segment(segment, clip_path, temp_dir, index)
            timeline_paths.append(clip_path)
        timeline_paths.extend(demo_paths[next_insert:])

        joined_path = temp_dir / "joined_visuals.mp4"
        self.logger("Concatenating rendered visual segments...")
        self._concat_segments(timeline_paths, temp_dir, joined_path)

        narration_path = self.config.target_voice
        if inserts:
            narration_path = temp_dir / "narration_with_demo_gaps.wav"
            self.logger("Pausing the new narration at each demo insert...")
            self._build_gapped_narration(inserts, plan.target_duration, narration_path)

        final_duration = plan.final_duration
        output_path = run_dir / "final_video.mp4"
        self.logger("Muxing target narration...")
        self._mux_final(joined_path, output_path, target_srt, narration_path, final_duration)
        if self.config.export_srt:
            subtitle_output = run_dir / "final_video.srt"
            if target_srt.resolve() != subtitle_output.resolve():
                shutil.copy2(target_srt, subtitle_output)
            self.logger(f"Exported subtitles: {subtitle_output.name}")
        else:
            subtitle_output = run_dir / "final_video.srt"
            if subtitle_output.exists():
                subtitle_output.unlink()
                self.logger(
                    "Removed the previous final_video.srt so media players do not "
                    "auto-display stale subtitles."
                )

        info = probe_media(output_path)
        difference = abs(info.duration - final_duration)
        if difference > 1.0:
            raise RuntimeError(
                f"Final duration verification failed: video={info.duration:.3f}s, "
                f"expected={final_duration:.3f}s (target voice {plan.target_duration:.3f}s "
                f"+ demo {plan.demo_duration:.3f}s)."
            )
        self.logger(
            f"Final verification passed: {info.duration:.3f}s "
            f"(target voice {plan.target_duration:.3f}s + demo {plan.demo_duration:.3f}s)."
        )
        return output_path

    def _render_demo_insert(self, item: DemoInsert, output_path: Path, index: int) -> None:
        duration = item.duration
        video_filter = self._video_chain(
            "[0:v]", duration, {"fitMode": "contain"}, source_duration=duration, speed=1.0, hold=0.0
        )
        audio_filter = (
            f"[0:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
            f"volume={item.volume:.4f},apad,atrim=duration={duration:.3f},"
            "aresample=48000,aformat=channel_layouts=stereo[aout]"
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{item.source_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(self.config.source_video),
            "-filter_complex",
            video_filter + ";[vbase]null[vout];" + audio_filter,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *self._clip_encode_args(duration),
            str(output_path),
        ]
        self._run(command, f"demo insert {index}")

    def _build_gapped_narration(
        self, inserts: Sequence[DemoInsert], target_duration: float, output_path: Path
    ) -> None:
        """Split the narration at each insert point and put silence of the demo length there."""
        pieces: list[str] = []
        filters: list[str] = []
        cursor = 0.0
        for item in inserts:
            point = min(target_duration, max(cursor, item.insert_at))
            if point > cursor + 0.001:
                label = f"n{len(pieces)}"
                filters.append(
                    f"[0:a]atrim=start={cursor:.3f}:end={point:.3f},asetpts=PTS-STARTPTS,"
                    f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[{label}]"
                )
                pieces.append(f"[{label}]")
            label = f"s{len(pieces)}"
            filters.append(
                f"aevalsrc=0|0:c=stereo:s=48000:d={item.duration:.3f},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[{label}]"
            )
            pieces.append(f"[{label}]")
            cursor = point
        if target_duration > cursor + 0.001:
            label = f"n{len(pieces)}"
            filters.append(
                f"[0:a]atrim=start={cursor:.3f},asetpts=PTS-STARTPTS,"
                f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[{label}]"
            )
            pieces.append(f"[{label}]")
        filters.append("".join(pieces) + f"concat=n={len(pieces)}:v=0:a=1[aout]")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(self.config.target_voice),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[aout]",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        self._run(command, "narration with demo gaps")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create the narration with demo gaps.")

    def _clip_encode_args(self, duration: float) -> list[str]:
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self.fps),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
        ]

    def _render_segment(
        self,
        segment: EditSegment,
        output_path: Path,
        temp_dir: Path,
        index: int,
    ) -> None:
        visual = segment.visual
        visual_type = visual["type"]
        duration = segment.duration
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        audio_source_index = 1

        if visual_type == "source_video":
            source_duration = float(visual["sourceEnd"]) - float(visual["sourceStart"])
            command.extend(
                [
                    "-ss",
                    f"{float(visual['sourceStart']):.3f}",
                    "-t",
                    f"{source_duration:.3f}",
                    "-i",
                    str(self.config.source_video),
                ]
            )
            speed = float(visual.get("playbackSpeed", 1.0))
            hold = float(visual.get("holdLastFrameSeconds", 0.0))
            video_filter = self._video_chain(
                "[0:v]",
                duration,
                visual,
                source_duration=source_duration,
                speed=speed,
                hold=hold,
            )
        elif visual_type == "source_freeze_frame":
            command.extend(
                [
                    "-ss",
                    f"{float(visual['sourceTime']):.3f}",
                    "-t",
                    "0.080",
                    "-i",
                    str(self.config.source_video),
                ]
            )
            video_filter = self._freeze_chain("[0:v]", duration, visual)
        elif visual_type == "asset_image":
            asset = self.assets[visual["assetId"]]
            command.extend(["-loop", "1", "-i", str(asset.path)])
            video_filter = self._image_chain("[0:v]", duration, visual)
        elif visual_type == "asset_video":
            asset = self.assets[visual["assetId"]]
            source_duration = float(visual["assetEnd"]) - float(visual["assetStart"])
            command.extend(
                [
                    "-ss",
                    f"{float(visual['assetStart']):.3f}",
                    "-t",
                    f"{source_duration:.3f}",
                    "-i",
                    str(asset.path),
                ]
            )
            video_filter = self._video_chain(
                "[0:v]",
                duration,
                visual,
                source_duration=source_duration,
                speed=float(visual.get("playbackSpeed", 1.0)),
                hold=float(visual.get("holdLastFrameSeconds", 0.0)),
            )
        else:
            color = str(visual.get("color", "#111111"))
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s={self.width}x{self.height}:r={self.fps}:d={duration:.3f}",
                ]
            )
            video_filter = f"[0:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS[vbase]"

        command.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )
        overlay_filter = self._overlay_filter(segment, temp_dir, index)
        video_filter += overlay_filter

        use_source_audio = (
            visual_type == "source_video"
            and self.config.original_audio_mode == "background"
            and self.source_info.has_audio
        )
        if use_source_audio:
            speed = float(visual.get("playbackSpeed", 1.0))
            source_duration = float(visual["sourceEnd"]) - float(visual["sourceStart"])
            audio_filter = (
                f"[0:a]atrim=duration={source_duration:.3f},asetpts=PTS-STARTPTS,"
                f"atempo={speed:.5f},volume={self.config.original_audio_volume:.4f},"
                f"apad,atrim=duration={duration:.3f},"
                "aresample=48000,aformat=channel_layouts=stereo[aout]"
            )
        else:
            audio_filter = (
                f"[{audio_source_index}:a]atrim=duration={duration:.3f},"
                "asetpts=PTS-STARTPTS[aout]"
            )
        filter_complex = video_filter + ";" + audio_filter
        command.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                *self._clip_encode_args(duration),
                str(output_path),
            ]
        )
        self._run(command, f"segment {index}")

    def _video_chain(
        self,
        input_label: str,
        duration: float,
        visual: dict[str, Any],
        source_duration: float,
        speed: float,
        hold: float,
    ) -> str:
        filters = [
            f"trim=duration={source_duration:.3f}",
            "setpts=PTS-STARTPTS",
            f"setpts=PTS/{speed:.6f}",
            f"fps={self.fps}",
        ]
        if hold > 0:
            filters.append(f"tpad=stop_mode=clone:stop_duration={hold + 0.08:.3f}")
        filters.extend([f"trim=duration={duration:.3f}", "setpts=PTS-STARTPTS"])
        filters.extend(self._fit_filters(visual.get("fitMode", "contain")))
        filters.extend(self._zoom_filters(visual.get("zoom"), duration))
        return input_label + ",".join(filters) + "[vbase]"

    def _freeze_chain(
        self, input_label: str, duration: float, visual: dict[str, Any]
    ) -> str:
        filters = ["trim=end=0.05", r"select=eq(n\,0)", "setpts=PTS-STARTPTS"]
        filters.extend(self._fit_filters(visual.get("fitMode", "contain")))
        filters.extend(
            [
                f"tpad=stop_mode=clone:stop_duration={duration + 0.08:.3f}",
                f"trim=duration={duration:.3f}",
                f"fps={self.fps}",
                "setpts=PTS-STARTPTS",
            ]
        )
        return input_label + ",".join(filters) + "[vbase]"

    def _image_chain(
        self, input_label: str, duration: float, visual: dict[str, Any]
    ) -> str:
        filters = [f"trim=duration={duration:.3f}", f"fps={self.fps}"]
        filters.extend(self._fit_filters(visual.get("fitMode", "contain")))
        motion = visual.get("motion", {})
        if isinstance(motion, dict) and motion.get("type") == "slow_zoom":
            filters.extend(
                self._zoom_filters(
                    {
                        "enabled": True,
                        "startScale": motion.get("startScale", 1.0),
                        "endScale": motion.get("endScale", 1.05),
                    },
                    duration,
                )
            )
        filters.extend([f"trim=duration={duration:.3f}", "setpts=PTS-STARTPTS"])
        return input_label + ",".join(filters) + "[vbase]"

    def _fit_filters(self, fit_mode: object) -> list[str]:
        if fit_mode == "cover":
            return [
                f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase",
                f"crop={self.width}:{self.height}",
                "setsar=1",
            ]
        return [
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease",
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
        ]

    def _zoom_filters(self, zoom: object, duration: float) -> list[str]:
        if not isinstance(zoom, dict) or not zoom.get("enabled"):
            return []
        start = float(zoom.get("startScale", 1.0))
        end = float(zoom.get("endScale", 1.05))
        denominator = max(1, int(round(duration * self.fps)) - 1)
        expression = f"{start:.5f}+({end - start:.5f})*on/{denominator}"
        return [
            "zoompan="
            f"z='{expression}':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={self.width}x{self.height}:fps={self.fps}"
        ]

    def _overlay_filter(self, segment: EditSegment, temp_dir: Path, index: int) -> str:
        overlay = segment.overlay
        if overlay.get("type") != "text":
            return ";[vbase]null[vout]"
        text_path = temp_dir / f"overlay_{index:04d}.txt"
        text_path.write_text(str(overlay["text"]), encoding="utf-8")
        escaped_path = escape_filter_path(text_path)
        start = float(overlay.get("startOffset", 0.0))
        end = start + float(overlay.get("duration", segment.duration))
        position = overlay.get("position", "top")
        y = {
            "top": "h*0.08",
            "center": "(h-text_h)/2",
            "bottom": "h-text_h-h*0.10",
        }.get(position, "h*0.08")
        font_size = max(24, int(round(self.height * 0.052)))
        animation = overlay.get("animation", "none")
        alpha = "1"
        if animation in {"fade", "pop"}:
            fade = min(0.2, max(0.05, (end - start) / 4))
            alpha = (
                f"if(lt(t,{start + fade:.3f}),(t-{start:.3f})/{fade:.3f},"
                f"if(gt(t,{end - fade:.3f}),({end:.3f}-t)/{fade:.3f},1))"
            )
        return (
            ";[vbase]drawtext="
            f"textfile='{escaped_path}':reload=0:fontcolor=white:fontsize={font_size}:"
            "borderw=2:bordercolor=black:box=1:boxcolor=black@0.42:boxborderw=14:"
            f"x=(w-text_w)/2:y={y}:alpha='{alpha}':"
            f"enable='between(t,{start:.3f},{end:.3f})'[vout]"
        )

    def _concat_segments(
        self, segment_paths: Sequence[Path], temp_dir: Path, output_path: Path
    ) -> None:
        concat_file = temp_dir / "segments.ffconcat"
        lines = ["ffconcat version 1.0"]
        lines.extend(f"file '{escape_concat_path(path)}'" for path in segment_paths)
        concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        copy_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        try:
            self._run(copy_command, "segment concat")
        except RuntimeError as exc:
            self.logger(f"Stream-copy concat failed; retrying with re-encode: {exc}")
            reencode_command = copy_command[:-5] + [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
            self._run(reencode_command, "segment concat re-encode")

    def _mux_final(
        self,
        joined_path: Path,
        output_path: Path,
        target_srt: Path,
        narration_path: Path,
        final_duration: float,
    ) -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(joined_path),
            "-i",
            str(narration_path),
        ]
        # The joined clips already carry the per-segment original audio: silence
        # when muted, quiet background, and full-volume demo inserts.
        audio_filter = (
            "[1:a]aresample=48000,aformat=channel_layouts=stereo[narration];"
            "[narration][0:a]amix=inputs=2:duration=longest:"
            "dropout_transition=0:normalize=0[mixed]"
        )
        audio_map = "[mixed]"
        if self.config.normalize_audio:
            audio_filter += ";[mixed]loudnorm=I=-16:LRA=11:TP=-1.5[normalized]"
            audio_map = "[normalized]"
        command.extend(["-filter_complex", audio_filter])

        if self.config.burn_subtitles:
            escaped_srt = escape_filter_path(target_srt)
            style = (
                f"FontName=Arial,FontSize={self.config.subtitle_font_size},"
                "PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
                "Alignment=2,MarginV=40,MarginL=30,MarginR=30"
            )
            command.extend(
                [
                    "-vf",
                    f"subtitles=filename='{escaped_srt}':force_style='{style}'",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
        else:
            command.extend(["-c:v", "copy"])
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                audio_map,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-t",
                f"{final_duration:.3f}",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        self._run(command, "final mux")

    def _run(self, command: list[str], label: str) -> None:
        self._check_cancelled()
        self.logger(f"FFmpeg [{label}]: {' '.join(command[:12])} ...")
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while process.poll() is None:
            if self.cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise InterruptedError("Stopped by user.")
            time.sleep(0.2)
        stderr = process.stderr.read() if process.stderr else ""
        if process.returncode != 0:
            raise RuntimeError(
                f"FFmpeg {label} failed ({process.returncode}): {stderr.strip()}"
            )
        output = Path(command[-1])
        if output.suffix.lower() == ".mp4" and (
            not output.exists() or output.stat().st_size == 0
        ):
            raise RuntimeError(f"FFmpeg {label} did not create a usable output file.")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise InterruptedError("Stopped by user.")
