"""One-file, multi-platform desktop video/audio downloader.

Runtime dependencies:
    pip install PySide6 yt-dlp

FFmpeg is required for MP4 merging and audio conversion. Put yt-dlp.exe and
ffmpeg.exe in a ``bin`` folder next to this file/executable, or add them to PATH.

Example Windows build:
    pyinstaller --noconsole --onefile --name VideoDownloader main.py

Bundle local tools when desired:
    pyinstaller --noconsole --onefile --name VideoDownloader ^
      --add-binary "bin/yt-dlp.exe;bin" --add-binary "bin/ffmpeg.exe;bin" main.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    from PySide6.QtCore import (
        QObject,
        QSettings,
        QStandardPaths,
        Qt,
        QThread,
        QTimer,
        QUrl,
        Signal,
    )
    from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QPixmap
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QRadioButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # Friendly terminal error before Qt is available.
    print(
        "Missing dependency: PySide6. Install it with:\n\n"
        "    python -m pip install PySide6 yt-dlp\n",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


APP_NAME = "Video Downloader"
ORG_NAME = "VideoAI"
MAX_CONCURRENT_DOWNLOADS = 2
PROGRESS_PREFIX = "__VD_PROGRESS__"
PROCESSING_PREFIX = "__VD_PROCESSING__"
FILE_PREFIX = "__VD_FILE__"
META_PREFIX = "__VD_META__"

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "app_title": "Video Downloader",
        "subtitle": "Download video or audio from sites supported by yt-dlp",
        "video_url": "Video URL",
        "url_placeholder": "Paste one or more video URLs (one per line)…",
        "paste": "Paste",
        "analyze": "Analyze",
        "analyzing": "Analyzing…",
        "show_details": "Show details",
        "no_preview": "No preview",
        "preview_hint": "Analyze a URL to see video details",
        "reading_info": "Reading video information…",
        "could_not_analyze": "Could not analyze this URL",
        "uploader_empty": "Uploader: —",
        "uploader": "Uploader: {name}",
        "platform_empty": "Platform: —  •  Duration: —",
        "platform_duration": "Platform: {platform}  •  Duration: {duration}",
        "loading": "Loading…",
        "no_thumbnail": "No thumbnail",
        "thumbnail_unavailable": "Thumbnail unavailable",
        "download_options": "Download options",
        "video": "Video",
        "audio": "Audio",
        "quality": "Quality",
        "format": "Format",
        "audio_quality": "Audio quality",
        "best": "Best",
        "save_to": "Save to",
        "browse": "Browse",
        "cookies_title": "Cookies / authenticated websites",
        "use_browser_cookies": "Use browser cookies",
        "import_cookies": "Import cookies.txt",
        "clear": "Clear",
        "no_cookie_file": "No cookies.txt selected",
        "download": "DOWNLOAD",
        "downloads": "Downloads",
        "concurrency": "Up to 2 concurrent downloads",
        "empty_queue": "No downloads yet",
        "cancel": "Cancel",
        "retry": "Retry",
        "open_folder": "Open Folder",
        "remove": "Remove",
        "preparing": "Preparing download…",
        "preparing_site": "Preparing {site} download…",
        "website": "Website",
        "unknown": "Unknown",
        "untitled_video": "Untitled video",
        "eta": "ETA {value}",
        "completed_stats": "100%  •  Completed",
        "status_waiting": "Waiting",
        "status_downloading": "Downloading",
        "status_processing": "Processing",
        "status_completed": "Completed",
        "status_failed": "Failed",
        "status_cancelled": "Cancelled",
        "ytdlp_missing": "yt-dlp missing",
        "ffmpeg_missing_short": "FFmpeg missing",
        "not_found": "not found",
        "select_download_folder": "Select download folder",
        "import_cookies_title": "Import Netscape cookies.txt",
        "cookies_filter": "Netscape cookies (cookies.txt *.txt);;All files (*.*)",
        "invalid_url_title": "Invalid URL",
        "invalid_url": "Enter at least one valid http:// or https:// URL.",
        "invalid_video_url": "Enter a valid http:// or https:// video URL.",
        "folder_unavailable_title": "Download folder unavailable",
        "folder_unavailable": "Choose a writable download folder.",
        "analysis_details": "Analysis details",
        "no_technical_details": "No additional technical details were reported.",
        "status_line": "Status: {status}",
        "exit_title": "Exit Video Downloader?",
        "exit_question": "Active and queued downloads will be cancelled. Exit now?",
        "msg_ffmpeg_install": "FFmpeg was not found. Install it or put ffmpeg.exe in the bin folder.",
        "msg_private": "This video is private or requires authentication.",
        "msg_login": "This website requires login. Try enabling browser cookies.",
        "msg_unsupported": "This website or URL is not currently supported.",
        "msg_unavailable": "This video is unavailable.",
        "msg_network": "Download failed because of a network error. Please try again.",
        "msg_quality": "The selected quality is unavailable. Try Best quality instead.",
        "msg_cookie_read": "Browser cookies could not be read. Close the browser and try again.",
        "msg_permission": "The destination is not writable. Choose another download folder.",
        "msg_generic": "The operation failed. Open Show details for technical information.",
        "msg_ytdlp": "yt-dlp is not installed.",
        "msg_ytdlp_job": "yt-dlp is not installed. Install yt-dlp or add bin/yt-dlp.exe.",
        "msg_ffmpeg": "FFmpeg was not found.",
        "msg_cancelled": "Download cancelled.",
        "msg_cancelling": "Cancelling…",
        "msg_analysis_cancelled": "Analysis cancelled.",
        "msg_invalid_metadata": "The website returned invalid metadata.",
    },
    "vi": {
        "app_title": "Trình tải Video",
        "subtitle": "Tải video hoặc âm thanh từ các trang được yt-dlp hỗ trợ",
        "video_url": "Đường dẫn video",
        "url_placeholder": "Dán một hoặc nhiều đường dẫn video (mỗi dòng một URL)…",
        "paste": "Dán",
        "analyze": "Phân tích",
        "analyzing": "Đang phân tích…",
        "show_details": "Xem chi tiết",
        "no_preview": "Chưa có xem trước",
        "preview_hint": "Phân tích URL để xem thông tin video",
        "reading_info": "Đang đọc thông tin video…",
        "could_not_analyze": "Không thể phân tích URL này",
        "uploader_empty": "Kênh đăng: —",
        "uploader": "Kênh đăng: {name}",
        "platform_empty": "Nền tảng: —  •  Thời lượng: —",
        "platform_duration": "Nền tảng: {platform}  •  Thời lượng: {duration}",
        "loading": "Đang tải…",
        "no_thumbnail": "Không có ảnh thu nhỏ",
        "thumbnail_unavailable": "Không tải được ảnh thu nhỏ",
        "download_options": "Tùy chọn tải xuống",
        "video": "Video",
        "audio": "Âm thanh",
        "quality": "Chất lượng",
        "format": "Định dạng",
        "audio_quality": "Chất lượng âm thanh",
        "best": "Tốt nhất",
        "save_to": "Lưu vào",
        "browse": "Chọn thư mục",
        "cookies_title": "Cookies / trang web cần đăng nhập",
        "use_browser_cookies": "Dùng cookies từ trình duyệt",
        "import_cookies": "Nhập cookies.txt",
        "clear": "Xóa",
        "no_cookie_file": "Chưa chọn cookies.txt",
        "download": "TẢI XUỐNG",
        "downloads": "Danh sách tải xuống",
        "concurrency": "Tối đa 2 tác vụ cùng lúc",
        "empty_queue": "Chưa có tác vụ tải xuống",
        "cancel": "Hủy",
        "retry": "Thử lại",
        "open_folder": "Mở thư mục",
        "remove": "Xóa",
        "preparing": "Đang chuẩn bị tải…",
        "preparing_site": "Đang chuẩn bị tải từ {site}…",
        "website": "Trang web",
        "unknown": "Không rõ",
        "untitled_video": "Video chưa có tiêu đề",
        "eta": "Còn lại {value}",
        "completed_stats": "100%  •  Hoàn tất",
        "status_waiting": "Đang chờ",
        "status_downloading": "Đang tải",
        "status_processing": "Đang xử lý",
        "status_completed": "Hoàn tất",
        "status_failed": "Thất bại",
        "status_cancelled": "Đã hủy",
        "ytdlp_missing": "Thiếu yt-dlp",
        "ffmpeg_missing_short": "Thiếu FFmpeg",
        "not_found": "không tìm thấy",
        "select_download_folder": "Chọn thư mục tải xuống",
        "import_cookies_title": "Nhập cookies.txt định dạng Netscape",
        "cookies_filter": "Netscape cookies (cookies.txt *.txt);;Tất cả tệp (*.*)",
        "invalid_url_title": "URL không hợp lệ",
        "invalid_url": "Hãy nhập ít nhất một URL http:// hoặc https:// hợp lệ.",
        "invalid_video_url": "Hãy nhập URL video http:// hoặc https:// hợp lệ.",
        "folder_unavailable_title": "Không dùng được thư mục tải xuống",
        "folder_unavailable": "Hãy chọn thư mục có quyền ghi.",
        "analysis_details": "Chi tiết phân tích",
        "no_technical_details": "Không có thêm thông tin kỹ thuật.",
        "status_line": "Trạng thái: {status}",
        "exit_title": "Thoát Trình tải Video?",
        "exit_question": "Các tác vụ đang chạy và đang chờ sẽ bị hủy. Bạn có muốn thoát?",
        "msg_ffmpeg_install": "Không tìm thấy FFmpeg. Hãy cài đặt hoặc đặt ffmpeg.exe trong thư mục bin.",
        "msg_private": "Video này ở chế độ riêng tư hoặc yêu cầu xác thực.",
        "msg_login": "Trang web này yêu cầu đăng nhập. Hãy thử bật cookies từ trình duyệt.",
        "msg_unsupported": "Trang web hoặc URL này hiện chưa được hỗ trợ.",
        "msg_unavailable": "Video này hiện không khả dụng.",
        "msg_network": "Tải xuống thất bại do lỗi mạng. Vui lòng thử lại.",
        "msg_quality": "Không có chất lượng đã chọn. Hãy thử chọn Tốt nhất.",
        "msg_cookie_read": "Không thể đọc cookies trình duyệt. Hãy đóng trình duyệt rồi thử lại.",
        "msg_permission": "Không có quyền ghi vào thư mục đích. Hãy chọn thư mục khác.",
        "msg_generic": "Thao tác thất bại. Mở Xem chi tiết để xem thông tin kỹ thuật.",
        "msg_ytdlp": "Chưa cài đặt yt-dlp.",
        "msg_ytdlp_job": "Chưa cài đặt yt-dlp. Hãy cài yt-dlp hoặc thêm bin/yt-dlp.exe.",
        "msg_ffmpeg": "Không tìm thấy FFmpeg.",
        "msg_cancelled": "Đã hủy tải xuống.",
        "msg_cancelling": "Đang hủy…",
        "msg_analysis_cancelled": "Đã hủy phân tích.",
        "msg_invalid_metadata": "Trang web trả về metadata không hợp lệ.",
    },
}

STATUS_KEYS = {
    "Waiting": "status_waiting",
    "Downloading": "status_downloading",
    "Processing": "status_processing",
    "Completed": "status_completed",
    "Failed": "status_failed",
    "Cancelled": "status_cancelled",
}

MESSAGE_KEYS = {
    TRANSLATIONS["en"][key]: key
    for key in (
        "msg_ffmpeg_install",
        "msg_private",
        "msg_login",
        "msg_unsupported",
        "msg_unavailable",
        "msg_network",
        "msg_quality",
        "msg_cookie_read",
        "msg_permission",
        "msg_generic",
        "msg_ytdlp",
        "msg_ytdlp_job",
        "msg_ffmpeg",
        "msg_cancelled",
        "msg_cancelling",
        "msg_analysis_cancelled",
        "msg_invalid_metadata",
        "invalid_video_url",
    )
}


def translate(language: str, key: str, **values: Any) -> str:
    table = TRANSLATIONS.get(language, TRANSLATIONS["en"])
    text = table.get(key, TRANSLATIONS["en"].get(key, key))
    return text.format(**values) if values else text


def translate_status(language: str, status: str) -> str:
    return translate(language, STATUS_KEYS.get(status, status))


def translate_message(language: str, message: str) -> str:
    key = MESSAGE_KEYS.get(message)
    return translate(language, key) if key else message

logger = logging.getLogger("video_downloader")


def setup_logging() -> Path:
    """Write useful diagnostics to a per-user folder that is normally writable."""
    base = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
    )
    base.mkdir(parents=True, exist_ok=True)
    log_path = base / "video_downloader.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return log_path


def resource_roots() -> list[Path]:
    """Return resource roots for source runs, onedir, and PyInstaller onefile."""
    roots: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def popen_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        options["start_new_session"] = True
    return options


def terminate_process_tree(process: Optional[subprocess.Popen[str]]) -> None:
    """Terminate yt-dlp and any FFmpeg child process it started."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                **popen_options(),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def friendly_error(raw_error: str) -> str:
    text = raw_error.lower()
    if "ffmpeg" in text and ("not found" in text or "not installed" in text):
        return "FFmpeg was not found. Install it or put ffmpeg.exe in the bin folder."
    if "private video" in text or "this video is private" in text:
        return "This video is private or requires authentication."
    if any(token in text for token in ("sign in", "login required", "log in", "cookies are needed")):
        return "This website requires login. Try enabling browser cookies."
    if "unsupported url" in text or "no suitable extractor" in text:
        return "This website or URL is not currently supported."
    if any(token in text for token in ("video unavailable", "not available", "has been removed")):
        return "This video is unavailable."
    if any(
        token in text
        for token in (
            "timed out",
            "temporary failure",
            "connection reset",
            "network is unreachable",
            "unable to download webpage",
            "http error 429",
            "http error 502",
            "http error 503",
        )
    ):
        return "Download failed because of a network error. Please try again."
    if "requested format is not available" in text:
        return "The selected quality is unavailable. Try Best quality instead."
    if "could not copy chrome cookie database" in text or "cookie database" in text:
        return "Browser cookies could not be read. Close the browser and try again."
    if "permission denied" in text or "access is denied" in text:
        return "The destination is not writable. Choose another download folder."
    return "The operation failed. Open Show details for technical information."


def format_duration(seconds: Any) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number < 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return "—"


def format_speed(value: Any) -> str:
    try:
        return f"{format_bytes(float(value))}/s"
    except (TypeError, ValueError):
        return "—"


def format_eta(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "—"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def numeric(value: str, default: Optional[float] = None) -> Optional[float]:
    if not value or value.strip().lower() in {"na", "none", "null", "unknown", "—"}:
        return default
    try:
        return float(value.strip().replace("%", ""))
    except ValueError:
        return default


def extract_urls(text: str) -> list[str]:
    candidates = re.findall(r"https?://[^\s<>\"]+", text, flags=re.IGNORECASE)
    if not candidates and text.strip():
        candidates = [text.strip()]
    result: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip()
        parsed = urlparse(candidate)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc and candidate not in result:
            result.append(candidate)
    return result


class YtDlpService:
    """Centralizes executable discovery and every yt-dlp argument."""

    def __init__(self) -> None:
        self.command_prefix = self._find_command()

    @staticmethod
    def _find_command() -> Optional[list[str]]:
        names = ("yt-dlp.exe", "yt-dlp") if os.name == "nt" else ("yt-dlp", "yt-dlp.exe")
        for root in resource_roots():
            for name in names:
                for candidate in (root / "bin" / name, root / name):
                    if candidate.is_file():
                        return [str(candidate)]
        discovered = shutil.which("yt-dlp")
        if discovered:
            return [discovered]
        if not getattr(sys, "frozen", False) and importlib.util.find_spec("yt_dlp"):
            return [sys.executable, "-m", "yt_dlp"]
        return None

    @property
    def available(self) -> bool:
        return self.command_prefix is not None

    @property
    def display_path(self) -> str:
        return " ".join(self.command_prefix or [])

    def check_version(self) -> Optional[str]:
        if not self.command_prefix:
            return None
        try:
            completed = subprocess.run(
                [*self.command_prefix, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
                **popen_options(),
            )
            return completed.stdout.strip() if completed.returncode == 0 else None
        except Exception:
            return None

    @staticmethod
    def cookie_arguments(use_browser: bool, browser: str, cookie_file: str) -> list[str]:
        if cookie_file and Path(cookie_file).is_file():
            return ["--cookies", cookie_file]
        if use_browser:
            return ["--cookies-from-browser", browser.lower()]
        return []

    def analyze_command(
        self, url: str, use_browser: bool, browser: str, cookie_file: str
    ) -> list[str]:
        if not self.command_prefix:
            raise RuntimeError("yt-dlp was not found")
        return [
            *self.command_prefix,
            "--ignore-config",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            "--no-color",
            "--encoding",
            "utf-8",
            *self.cookie_arguments(use_browser, browser, cookie_file),
            "--",
            url,
        ]

    def download_command(self, job: "DownloadJob", ffmpeg_path: Optional[str]) -> list[str]:
        if not self.command_prefix:
            raise RuntimeError("yt-dlp was not found")
        output_template = str(Path(job.save_dir) / "%(title).180B [%(id)s].%(ext)s")
        command = [
            *self.command_prefix,
            "--ignore-config",
            "--newline",
            "--no-color",
            "--no-playlist",
            "--encoding",
            "utf-8",
            "--progress",
            "--progress-template",
            (
                "download:"
                f"{PROGRESS_PREFIX}\t%(progress._percent_str)s"
                "\t%(progress.downloaded_bytes)s\t%(progress.total_bytes)s"
                "\t%(progress.total_bytes_estimate)s\t%(progress.speed)s"
                "\t%(progress.eta)s\t%(progress.status)s"
            ),
            "--progress-template",
            f"postprocess:{PROCESSING_PREFIX}\t%(progress.status)s",
            "--print",
            f"before_dl:{META_PREFIX}\t%(title)j\t%(extractor_key)j",
            "--print",
            f"after_move:{FILE_PREFIX}%(filepath)s",
            "-o",
            output_template,
        ]
        if os.name == "nt":
            command.append("--windows-filenames")
        if ffmpeg_path:
            command.extend(["--ffmpeg-location", ffmpeg_path])
        command.extend(self.cookie_arguments(job.use_browser_cookies, job.browser, job.cookie_file))

        if job.mode == "audio":
            quality_map = {
                "Best": "0",
                "320 kbps": "320K",
                "192 kbps": "192K",
                "128 kbps": "128K",
            }
            command.extend(
                [
                    "--extract-audio",
                    "--audio-format",
                    job.audio_format.lower(),
                    "--audio-quality",
                    quality_map.get(job.audio_quality, "0"),
                    "--add-metadata",
                ]
            )
        else:
            height = None if job.video_quality == "Best" else job.video_quality.removesuffix("p")
            if height:
                selector = (
                    f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
                    f"b[height<={height}][ext=mp4]/"
                    f"bv*[height<={height}]+ba/b[height<={height}]/b"
                )
            else:
                selector = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
            command.extend(
                [
                    "--format",
                    selector,
                    "--merge-output-format",
                    "mp4",
                    "--remux-video",
                    "mp4",
                    "--add-metadata",
                ]
            )
        command.extend(["--", job.url])
        return command


class FFmpegService:
    def __init__(self) -> None:
        self.path = self._find_ffmpeg()

    @staticmethod
    def _find_ffmpeg() -> Optional[str]:
        names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg", "ffmpeg.exe")
        for root in resource_roots():
            for name in names:
                for candidate in (root / "bin" / name, root / name):
                    if candidate.is_file():
                        return str(candidate)
        return shutil.which("ffmpeg")

    @property
    def available(self) -> bool:
        return self.path is not None


@dataclass
class DownloadJob:
    url: str
    title: str
    platform: str
    mode: str
    video_quality: str
    audio_format: str
    audio_quality: str
    save_dir: str
    use_browser_cookies: bool
    browser: str
    cookie_file: str
    title_pending: bool = False
    job_id: str = ""
    status: str = "Waiting"
    progress: float = 0.0
    downloaded: Optional[float] = None
    total: Optional[float] = None
    speed: Optional[float] = None
    eta: Optional[float] = None
    output_path: str = ""
    error: str = ""
    details: str = ""

    def __post_init__(self) -> None:
        if not self.job_id:
            self.job_id = uuid.uuid4().hex


class AnalyzeWorker(QObject):
    finished = Signal(bool, object, str, str)

    def __init__(
        self,
        service: YtDlpService,
        url: str,
        use_browser: bool,
        browser: str,
        cookie_file: str,
    ) -> None:
        super().__init__()
        self.service = service
        self.url = url
        self.use_browser = use_browser
        self.browser = browser
        self.cookie_file = cookie_file
        self._process: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        terminate_process_tree(process)

    def run(self) -> None:
        if not self.service.available:
            detail = "yt-dlp was not found in bin, PATH, or the active Python environment."
            self.finished.emit(False, {}, "yt-dlp is not installed.", detail)
            return
        try:
            command = self.service.analyze_command(
                self.url, self.use_browser, self.browser, self.cookie_file
            )
            logger.info("Analyzing URL with yt-dlp")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=env,
                **popen_options(),
            )
            with self._lock:
                self._process = process
            stdout, stderr = process.communicate()
            with self._lock:
                self._process = None
            if self._cancelled.is_set():
                self.finished.emit(False, {}, "Analysis cancelled.", "Analysis was cancelled.")
                return
            if process.returncode != 0:
                details = (stderr or stdout or "yt-dlp exited with an unknown error.").strip()
                self.finished.emit(False, {}, friendly_error(details), details)
                return
            metadata = json.loads(stdout)
            self.finished.emit(True, metadata, "", "")
        except json.JSONDecodeError as exc:
            details = f"Could not parse yt-dlp JSON output: {exc}"
            self.finished.emit(False, {}, "The website returned invalid metadata.", details)
        except Exception as exc:
            logger.exception("Analysis failed")
            details = f"{type(exc).__name__}: {exc}"
            self.finished.emit(False, {}, friendly_error(details), details)


class DownloadWorker(QObject):
    progress = Signal(str, float, object, object, object, object, str)
    metadata = Signal(str, str, str)
    finished = Signal(str, str, str, str, str)

    def __init__(
        self, service: YtDlpService, ffmpeg: FFmpegService, job: DownloadJob
    ) -> None:
        super().__init__()
        self.service = service
        self.ffmpeg = ffmpeg
        self.job = job
        self._process: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        terminate_process_tree(process)

    def _parse_progress(self, line: str) -> bool:
        position = line.find(PROGRESS_PREFIX)
        if position < 0:
            return False
        payload = line[position + len(PROGRESS_PREFIX) :].lstrip("\t")
        fields = payload.split("\t")
        fields.extend([""] * (7 - len(fields)))
        percent = numeric(fields[0], 0.0) or 0.0
        downloaded = numeric(fields[1])
        total = numeric(fields[2]) or numeric(fields[3])
        speed = numeric(fields[4])
        eta = numeric(fields[5])
        engine_status = fields[6].strip().lower()
        status = "Processing" if engine_status == "finished" else "Downloading"
        self.progress.emit(
            self.job.job_id, percent, downloaded, total, speed, eta, status
        )
        return True

    def _parse_metadata(self, line: str) -> bool:
        position = line.find(META_PREFIX)
        if position < 0:
            return False
        payload = line[position + len(META_PREFIX) :].lstrip("\t")
        parts = payload.split("\t", 1)
        try:
            title = json.loads(parts[0]) if parts and parts[0] else self.job.title
        except json.JSONDecodeError:
            title = parts[0] if parts else self.job.title
        try:
            platform = json.loads(parts[1]) if len(parts) > 1 and parts[1] else self.job.platform
        except json.JSONDecodeError:
            platform = parts[1] if len(parts) > 1 else self.job.platform
        self.metadata.emit(self.job.job_id, str(title or self.job.title), str(platform or "Website"))
        return True

    def run(self) -> None:
        details: list[str] = []
        output_path = ""
        try:
            command = self.service.download_command(self.job, self.ffmpeg.path)
            logger.info("Starting %s download (%s)", self.job.mode, self.job.job_id[:8])
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=env,
                **popen_options(),
            )
            with self._lock:
                self._process = process
            assert process.stdout is not None
            for raw_line in iter(process.stdout.readline, ""):
                line = raw_line.rstrip("\r\n")
                if self._cancelled.is_set():
                    terminate_process_tree(process)
                    break
                if self._parse_progress(line) or self._parse_metadata(line):
                    continue
                processing_pos = line.find(PROCESSING_PREFIX)
                if processing_pos >= 0:
                    self.progress.emit(
                        self.job.job_id, 100.0, self.job.total, self.job.total, None, None, "Processing"
                    )
                    continue
                file_pos = line.find(FILE_PREFIX)
                if file_pos >= 0:
                    output_path = line[file_pos + len(FILE_PREFIX) :].strip()
                    continue
                if line:
                    details.append(line)
                    if len(details) > 300:
                        details.pop(0)
            return_code = process.wait()
            with self._lock:
                self._process = None
            full_details = "\n".join(details).strip()
            if self._cancelled.is_set():
                logger.info("Download cancelled (%s)", self.job.job_id[:8])
                self.finished.emit(
                    self.job.job_id, "Cancelled", "Download cancelled.", full_details, output_path
                )
            elif return_code == 0:
                logger.info("Download completed (%s)", self.job.job_id[:8])
                self.finished.emit(self.job.job_id, "Completed", "", full_details, output_path)
            else:
                error = friendly_error(full_details)
                logger.warning("Download failed (%s): %s", self.job.job_id[:8], error)
                self.finished.emit(self.job.job_id, "Failed", error, full_details, output_path)
        except Exception as exc:
            with self._lock:
                self._process = None
            logger.exception("Download worker failed")
            full_details = "\n".join(details + [f"{type(exc).__name__}: {exc}"])
            status = "Cancelled" if self._cancelled.is_set() else "Failed"
            error = "Download cancelled." if status == "Cancelled" else friendly_error(full_details)
            self.finished.emit(self.job.job_id, status, error, full_details, output_path)


class DownloadManager(QObject):
    job_changed = Signal(str, object)
    job_removed = Signal(str)

    def __init__(self, service: YtDlpService, ffmpeg: FFmpegService) -> None:
        super().__init__()
        self.service = service
        self.ffmpeg = ffmpeg
        self.jobs: dict[str, DownloadJob] = {}
        self.waiting: list[str] = []
        self.workers: dict[str, tuple[QThread, DownloadWorker]] = {}

    def add(self, job: DownloadJob) -> None:
        self.jobs[job.job_id] = job
        self.waiting.append(job.job_id)
        self.job_changed.emit(job.job_id, job)
        QTimer.singleShot(0, self._pump)

    def _pump(self) -> None:
        while self.waiting and len(self.workers) < MAX_CONCURRENT_DOWNLOADS:
            job_id = self.waiting.pop(0)
            job = self.jobs.get(job_id)
            if not job or job.status != "Waiting":
                continue
            if not self.service.available:
                job.status = "Failed"
                job.error = "yt-dlp is not installed. Install yt-dlp or add bin/yt-dlp.exe."
                job.details = "yt-dlp was not found in bin, PATH, or the Python environment."
                self.job_changed.emit(job_id, job)
                continue
            if not self.ffmpeg.available:
                job.status = "Failed"
                job.error = "FFmpeg was not found."
                job.details = (
                    "FFmpeg is required to merge MP4 streams or convert audio. "
                    "Put ffmpeg.exe in the bin folder or add it to PATH."
                )
                self.job_changed.emit(job_id, job)
                continue
            Path(job.save_dir).mkdir(parents=True, exist_ok=True)
            thread = QThread(self)
            worker = DownloadWorker(self.service, self.ffmpeg, job)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.progress.connect(self._on_progress)
            worker.metadata.connect(self._on_metadata)
            worker.finished.connect(self._on_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(lambda jid=job_id: self._thread_done(jid))
            thread.finished.connect(thread.deleteLater)
            self.workers[job_id] = (thread, worker)
            job.status = "Downloading"
            self.job_changed.emit(job_id, job)
            thread.start()

    def _on_progress(
        self,
        job_id: str,
        percent: float,
        downloaded: Any,
        total: Any,
        speed: Any,
        eta: Any,
        status: str,
    ) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.progress = max(0.0, min(100.0, percent))
        job.downloaded = downloaded
        job.total = total
        job.speed = speed
        job.eta = eta
        job.status = status
        self.job_changed.emit(job_id, job)

    def _on_metadata(self, job_id: str, title: str, platform: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.title = title
        job.platform = platform
        job.title_pending = False
        self.job_changed.emit(job_id, job)

    def _on_finished(
        self, job_id: str, status: str, error: str, details: str, output_path: str
    ) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.status = status
        job.error = error
        job.details = details
        job.output_path = output_path
        job.speed = None
        job.eta = None
        if status == "Completed":
            job.progress = 100.0
            if job.total is not None:
                job.downloaded = job.total
        self.job_changed.emit(job_id, job)

    def _thread_done(self, job_id: str) -> None:
        self.workers.pop(job_id, None)
        QTimer.singleShot(0, self._pump)

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        if job_id in self.waiting:
            self.waiting.remove(job_id)
            job.status = "Cancelled"
            job.error = "Download cancelled."
            self.job_changed.emit(job_id, job)
            return
        pair = self.workers.get(job_id)
        if pair:
            job.error = "Cancelling…"
            self.job_changed.emit(job_id, job)
            pair[1].cancel()

    def retry(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job or job_id in self.workers or job.status not in {"Failed", "Cancelled"}:
            return
        job.status = "Waiting"
        job.progress = 0.0
        job.downloaded = None
        job.total = None
        job.speed = None
        job.eta = None
        job.output_path = ""
        job.error = ""
        job.details = ""
        if job_id not in self.waiting:
            self.waiting.append(job_id)
        self.job_changed.emit(job_id, job)
        QTimer.singleShot(0, self._pump)

    def remove(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job or job_id in self.workers or job.status in {"Waiting", "Downloading", "Processing"}:
            return
        if job_id in self.waiting:
            self.waiting.remove(job_id)
        del self.jobs[job_id]
        self.job_removed.emit(job_id)

    def has_active_jobs(self) -> bool:
        return bool(self.workers or self.waiting)

    def shutdown(self) -> None:
        self.waiting.clear()
        pairs = list(self.workers.values())
        for _thread, worker in pairs:
            worker.cancel()
        for thread, _worker in pairs:
            thread.quit()
            thread.wait(5000)


class DownloadJobWidget(QFrame):
    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    remove_requested = Signal(str)
    open_requested = Signal(str)
    details_requested = Signal(str)

    def __init__(self, job: DownloadJob, language: str) -> None:
        super().__init__()
        self.job_id = job.job_id
        self.language = language
        self.job = job
        self.setObjectName("jobCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setObjectName("jobTitle")
        self.title_label.setWordWrap(True)
        self.platform_label = QLabel()
        self.platform_label.setObjectName("muted")
        top.addWidget(self.title_label, 1)
        top.addWidget(self.platform_label)
        layout.addLayout(top)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        info = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("statusLabel")
        self.stats_label = QLabel()
        self.stats_label.setObjectName("muted")
        self.stats_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info.addWidget(self.status_label)
        info.addStretch(1)
        info.addWidget(self.stats_label)
        layout.addLayout(info)

        controls = QHBoxLayout()
        controls.setSpacing(7)
        self.cancel_button = QPushButton()
        self.retry_button = QPushButton()
        self.open_button = QPushButton()
        self.details_button = QPushButton()
        self.remove_button = QPushButton()
        for button in (
            self.cancel_button,
            self.retry_button,
            self.open_button,
            self.details_button,
            self.remove_button,
        ):
            button.setProperty("secondary", True)
            button.setCursor(Qt.PointingHandCursor)
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self.job_id))
        self.retry_button.clicked.connect(lambda: self.retry_requested.emit(self.job_id))
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.job_id))
        self.details_button.clicked.connect(lambda: self.details_requested.emit(self.job_id))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.job_id))
        self.retranslate()
        self.update_job(job)

    def tr(self, key: str, **values: Any) -> str:
        return translate(self.language, key, **values)

    def set_language(self, language: str) -> None:
        self.language = language
        self.retranslate()
        self.update_job(self.job)

    def retranslate(self) -> None:
        self.cancel_button.setText(self.tr("cancel"))
        self.retry_button.setText(self.tr("retry"))
        self.open_button.setText(self.tr("open_folder"))
        self.details_button.setText(self.tr("show_details"))
        self.remove_button.setText(self.tr("remove"))

    def update_job(self, job: DownloadJob) -> None:
        self.job = job
        if job.title_pending:
            displayed_title = self.tr("preparing_site", site=job.platform)
        else:
            displayed_title = job.title or self.tr("preparing")
        self.title_label.setText(displayed_title)
        self.title_label.setToolTip(job.title)
        self.platform_label.setText(job.platform or self.tr("website"))
        self.progress_bar.setValue(round(job.progress * 10))
        self.status_label.setText(
            translate_message(self.language, job.error)
            if job.error
            else translate_status(self.language, job.status)
        )
        if job.status in {"Downloading", "Processing"}:
            size = format_bytes(job.downloaded)
            total = format_bytes(job.total)
            self.stats_label.setText(
                f"{job.progress:.1f}%  •  {format_speed(job.speed)}  •  "
                f"{size} / {total}  •  {self.tr('eta', value=format_eta(job.eta))}"
            )
        elif job.status == "Completed":
            self.stats_label.setText(self.tr("completed_stats"))
        else:
            self.stats_label.setText(f"{job.progress:.1f}%")
        active = job.status in {"Waiting", "Downloading", "Processing"}
        self.cancel_button.setEnabled(active)
        self.retry_button.setEnabled(job.status in {"Failed", "Cancelled"})
        self.open_button.setEnabled(job.status == "Completed")
        self.details_button.setVisible(bool(job.details or job.error))
        self.remove_button.setEnabled(not active)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.language = self.settings.value("language", "vi", str)
        if self.language not in TRANSLATIONS:
            self.language = "vi"
        self.ytdlp = YtDlpService()
        self.ffmpeg = FFmpegService()
        self.manager = DownloadManager(self.ytdlp, self.ffmpeg)
        self.manager.job_changed.connect(self.on_job_changed)
        self.manager.job_removed.connect(self.on_job_removed)
        self.job_widgets: dict[str, DownloadJobWidget] = {}
        self.analysis_metadata: dict[str, Any] = {}
        self.analyzed_url = ""
        self.analysis_error_details = ""
        self.analysis_error_message = ""
        self.analysis_state = "idle"
        self.thumbnail_state = "empty"
        self.analyze_thread: Optional[QThread] = None
        self.analyze_worker: Optional[AnalyzeWorker] = None
        self.thumbnail_reply: Optional[QNetworkReply] = None
        self.network = QNetworkAccessManager(self)
        self.log_path = setup_logging()

        self.setWindowTitle(self.tr("app_title"))
        self.resize(980, 840)
        self.setMinimumSize(760, 650)
        self._build_ui()
        self._apply_style()
        self._restore_settings()
        self.retranslate_ui()
        self._refresh_tool_status()
        logger.info("Application started")

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        return card, layout

    def tr(self, key: str, **values: Any) -> str:
        return translate(self.language, key, **values)

    def _build_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.setCentralWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(30, 26, 30, 30)
        root.setSpacing(16)

        header = QHBoxLayout()
        heading_box = QVBoxLayout()
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        heading_box.addWidget(self.heading)
        heading_box.addWidget(self.subtitle)
        header.addLayout(heading_box)
        header.addStretch(1)
        self.tool_status = QLabel()
        self.tool_status.setObjectName("toolStatus")
        self.tool_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.tool_status)
        self.language_combo = QComboBox()
        self.language_combo.setMinimumWidth(125)
        self.language_combo.addItem("Tiếng Việt", "vi")
        self.language_combo.addItem("English", "en")
        language_index = self.language_combo.findData(self.language)
        self.language_combo.setCurrentIndex(max(0, language_index))
        self.language_combo.currentIndexChanged.connect(self.change_language)
        header.addWidget(self.language_combo)
        root.addLayout(header)

        url_card, url_layout = self._card()
        self.url_title = QLabel()
        self.url_title.setObjectName("sectionTitle")
        url_layout.addWidget(self.url_title)
        self.url_input = QPlainTextEdit()
        self.url_input.setFixedHeight(70)
        url_layout.addWidget(self.url_input)
        url_buttons = QHBoxLayout()
        url_buttons.addStretch(1)
        self.paste_button = QPushButton()
        self.paste_button.setProperty("secondary", True)
        self.analyze_button = QPushButton()
        self.paste_button.clicked.connect(self.paste_clipboard)
        self.analyze_button.clicked.connect(self.analyze_url)
        url_buttons.addWidget(self.paste_button)
        url_buttons.addWidget(self.analyze_button)
        url_layout.addLayout(url_buttons)
        self.analysis_status = QLabel()
        self.analysis_status.setObjectName("errorLabel")
        self.analysis_status.setWordWrap(True)
        self.analysis_status.hide()
        url_layout.addWidget(self.analysis_status)
        self.analysis_details_button = QPushButton()
        self.analysis_details_button.setProperty("secondary", True)
        self.analysis_details_button.setMaximumWidth(130)
        self.analysis_details_button.clicked.connect(self.show_analysis_details)
        self.analysis_details_button.hide()
        url_layout.addWidget(self.analysis_details_button, 0, Qt.AlignRight)
        root.addWidget(url_card)

        self.preview_card, preview_layout = self._card()
        preview_row = QHBoxLayout()
        preview_row.setSpacing(18)
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setObjectName("thumbnail")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setFixedSize(240, 135)
        preview_row.addWidget(self.thumbnail_label)
        metadata_layout = QVBoxLayout()
        metadata_layout.setSpacing(6)
        self.video_title = QLabel()
        self.video_title.setObjectName("previewTitle")
        self.video_title.setWordWrap(True)
        self.video_uploader = QLabel()
        self.video_uploader.setObjectName("muted")
        self.video_details = QLabel()
        self.video_details.setObjectName("muted")
        metadata_layout.addWidget(self.video_title)
        metadata_layout.addWidget(self.video_uploader)
        metadata_layout.addWidget(self.video_details)
        metadata_layout.addStretch(1)
        preview_row.addLayout(metadata_layout, 1)
        preview_layout.addLayout(preview_row)
        root.addWidget(self.preview_card)

        options_card, options_layout = self._card()
        self.options_title = QLabel()
        self.options_title.setObjectName("sectionTitle")
        options_layout.addWidget(self.options_title)
        mode_row = QHBoxLayout()
        self.video_radio = QRadioButton()
        self.audio_radio = QRadioButton()
        self.video_radio.setChecked(True)
        mode_row.addWidget(self.video_radio)
        mode_row.addWidget(self.audio_radio)
        mode_row.addStretch(1)
        options_layout.addLayout(mode_row)

        self.options_stack = QStackedWidget()
        video_page = QWidget()
        video_grid = QGridLayout(video_page)
        video_grid.setContentsMargins(0, 0, 0, 0)
        self.video_quality = QComboBox()
        for value in ("Best", "1080p", "720p", "480p", "360p"):
            self.video_quality.addItem(value, value)
        self.video_format = QComboBox()
        self.video_format.addItem("MP4", "MP4")
        self.video_quality_label = QLabel()
        self.video_format_label = QLabel()
        video_grid.addWidget(self.video_quality_label, 0, 0)
        video_grid.addWidget(self.video_quality, 1, 0)
        video_grid.addWidget(self.video_format_label, 0, 1)
        video_grid.addWidget(self.video_format, 1, 1)
        video_grid.setColumnStretch(0, 1)
        video_grid.setColumnStretch(1, 1)

        audio_page = QWidget()
        audio_grid = QGridLayout(audio_page)
        audio_grid.setContentsMargins(0, 0, 0, 0)
        self.audio_format = QComboBox()
        self.audio_format.addItem("MP3", "MP3")
        self.audio_format.addItem("M4A", "M4A")
        self.audio_quality = QComboBox()
        for value in ("Best", "320 kbps", "192 kbps", "128 kbps"):
            self.audio_quality.addItem(value, value)
        self.audio_format_label = QLabel()
        self.audio_quality_label = QLabel()
        audio_grid.addWidget(self.audio_format_label, 0, 0)
        audio_grid.addWidget(self.audio_format, 1, 0)
        audio_grid.addWidget(self.audio_quality_label, 0, 1)
        audio_grid.addWidget(self.audio_quality, 1, 1)
        audio_grid.setColumnStretch(0, 1)
        audio_grid.setColumnStretch(1, 1)
        self.options_stack.addWidget(video_page)
        self.options_stack.addWidget(audio_page)
        options_layout.addWidget(self.options_stack)
        self.video_radio.toggled.connect(
            lambda checked: self.options_stack.setCurrentIndex(0 if checked else 1)
        )

        self.path_label = QLabel()
        options_layout.addWidget(self.path_label)
        path_row = QHBoxLayout()
        self.save_path = QLineEdit()
        self.save_path.setReadOnly(True)
        self.browse_button = QPushButton()
        self.browse_button.setProperty("secondary", True)
        self.browse_button.clicked.connect(self.browse_folder)
        path_row.addWidget(self.save_path, 1)
        path_row.addWidget(self.browse_button)
        options_layout.addLayout(path_row)

        self.cookies_group = QGroupBox()
        cookies_layout = QGridLayout(self.cookies_group)
        self.browser_cookies = QCheckBox()
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(["Chrome", "Edge", "Firefox", "CocCoc"])
        self.browser_combo.setEnabled(False)
        self.browser_cookies.toggled.connect(self.browser_combo.setEnabled)
        self.import_cookies_button = QPushButton()
        self.import_cookies_button.setProperty("secondary", True)
        self.clear_cookies_button = QPushButton()
        self.clear_cookies_button.setProperty("secondary", True)
        self.cookie_file_label = QLabel()
        self.cookie_file_label.setObjectName("muted")
        self.cookie_file_label.setWordWrap(True)
        self.import_cookies_button.clicked.connect(self.import_cookies)
        self.clear_cookies_button.clicked.connect(self.clear_cookies)
        cookies_layout.addWidget(self.browser_cookies, 0, 0)
        cookies_layout.addWidget(self.browser_combo, 0, 1)
        cookies_layout.addWidget(self.import_cookies_button, 1, 0)
        cookies_layout.addWidget(self.clear_cookies_button, 1, 1)
        cookies_layout.addWidget(self.cookie_file_label, 2, 0, 1, 2)
        cookies_layout.setColumnStretch(0, 1)
        cookies_layout.setColumnStretch(1, 1)
        options_layout.addWidget(self.cookies_group)

        download_row = QHBoxLayout()
        download_row.addStretch(1)
        self.download_button = QPushButton()
        self.download_button.setObjectName("downloadButton")
        self.download_button.setMinimumWidth(230)
        self.download_button.clicked.connect(self.queue_downloads)
        download_row.addWidget(self.download_button)
        download_row.addStretch(1)
        options_layout.addLayout(download_row)
        root.addWidget(options_card)

        downloads_header = QHBoxLayout()
        self.downloads_title = QLabel()
        self.downloads_title.setObjectName("sectionTitle")
        self.concurrency_label = QLabel()
        self.concurrency_label.setObjectName("muted")
        downloads_header.addWidget(self.downloads_title)
        downloads_header.addStretch(1)
        downloads_header.addWidget(self.concurrency_label)
        root.addLayout(downloads_header)

        self.queue_container = QWidget()
        self.queue_layout = QVBoxLayout(self.queue_container)
        self.queue_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_layout.setSpacing(10)
        self.empty_queue = QLabel()
        self.empty_queue.setObjectName("emptyQueue")
        self.empty_queue.setAlignment(Qt.AlignCenter)
        self.empty_queue.setMinimumHeight(90)
        self.queue_layout.addWidget(self.empty_queue)
        root.addWidget(self.queue_container)
        root.addStretch(1)

        for button in (
            self.paste_button,
            self.analyze_button,
            self.browse_button,
            self.import_cookies_button,
            self.clear_cookies_button,
            self.analysis_details_button,
            self.download_button,
        ):
            button.setCursor(Qt.PointingHandCursor)

    def _apply_style(self) -> None:
        QApplication.instance().setFont(QFont("Segoe UI", 10))
        self.setStyleSheet(
            """
            QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {
                background: #f4f6fb;
                color: #172033;
            }
            QLabel#heading { font-size: 28px; font-weight: 700; color: #111827; }
            QLabel#sectionTitle { font-size: 17px; font-weight: 700; color: #111827; }
            QLabel#previewTitle { font-size: 18px; font-weight: 650; color: #111827; }
            QLabel#jobTitle { font-size: 14px; font-weight: 650; color: #111827; }
            QLabel#muted { color: #687386; }
            QLabel#errorLabel { color: #b42318; background: #fff1f0; border-radius: 7px; padding: 9px; }
            QLabel#toolStatus { color: #526072; background: #e8ecf4; padding: 7px 10px; border-radius: 8px; }
            QLabel#emptyQueue { color: #8a94a6; border: 1px dashed #cbd2df; border-radius: 10px; }
            QLabel#thumbnail { color: #8893a5; background: #e8ecf4; border-radius: 10px; }
            QFrame#card, QFrame#jobCard {
                background: white;
                border: 1px solid #e1e6ef;
                border-radius: 12px;
            }
            QPlainTextEdit, QLineEdit, QComboBox {
                background: #ffffff;
                border: 1px solid #cfd6e3;
                border-radius: 8px;
                padding: 8px 10px;
                selection-background-color: #5b67f1;
            }
            QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus { border: 1px solid #5b67f1; }
            QComboBox { min-height: 22px; }
            QGroupBox {
                border: 1px solid #dfe4ed;
                border-radius: 9px;
                margin-top: 9px;
                padding: 12px 9px 7px 9px;
                font-weight: 600;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QPushButton {
                background: #5b67f1;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 9px 15px;
                font-weight: 600;
            }
            QPushButton:hover { background: #4b56da; }
            QPushButton:disabled { background: #c6ccda; color: #f8f9fb; }
            QPushButton[secondary="true"] {
                background: #eef1f7;
                color: #344054;
                border: 1px solid #d7dce6;
            }
            QPushButton[secondary="true"]:hover { background: #e2e7f0; }
            QPushButton[secondary="true"]:disabled { color: #a3abba; background: #f3f5f8; }
            QPushButton#downloadButton { font-size: 14px; padding: 12px 24px; }
            QProgressBar { background: #e8ecf3; border: none; border-radius: 5px; height: 9px; }
            QProgressBar::chunk { background: #5b67f1; border-radius: 5px; }
            QRadioButton, QCheckBox { spacing: 7px; }
            """
        )

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_combo_item_text(combo: QComboBox, value: str, text: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setItemText(index, text)

    def change_language(self, _index: int) -> None:
        language = self.language_combo.currentData()
        if language not in TRANSLATIONS or language == self.language:
            return
        self.language = language
        self.settings.setValue("language", language)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(self.tr("app_title"))
        self.heading.setText(self.tr("app_title"))
        self.subtitle.setText(self.tr("subtitle"))
        self.url_title.setText(self.tr("video_url"))
        self.url_input.setPlaceholderText(self.tr("url_placeholder"))
        self.paste_button.setText(self.tr("paste"))
        self.analyze_button.setText(
            self.tr("analyzing") if self.analysis_state == "loading" else self.tr("analyze")
        )
        self.analysis_details_button.setText(self.tr("show_details"))
        self.options_title.setText(self.tr("download_options"))
        self.video_radio.setText(self.tr("video"))
        self.audio_radio.setText(self.tr("audio"))
        self.video_quality_label.setText(self.tr("quality"))
        self.video_format_label.setText(self.tr("format"))
        self.audio_format_label.setText(self.tr("format"))
        self.audio_quality_label.setText(self.tr("audio_quality"))
        self._set_combo_item_text(self.video_quality, "Best", self.tr("best"))
        self._set_combo_item_text(self.audio_quality, "Best", self.tr("best"))
        self.path_label.setText(self.tr("save_to"))
        self.browse_button.setText(self.tr("browse"))
        self.cookies_group.setTitle(self.tr("cookies_title"))
        self.browser_cookies.setText(self.tr("use_browser_cookies"))
        self.import_cookies_button.setText(self.tr("import_cookies"))
        self.clear_cookies_button.setText(self.tr("clear"))
        if not self._cookie_file():
            self.cookie_file_label.setText(self.tr("no_cookie_file"))
        self.download_button.setText(self.tr("download"))
        self.downloads_title.setText(self.tr("downloads"))
        self.concurrency_label.setText(self.tr("concurrency"))
        self.empty_queue.setText(self.tr("empty_queue"))

        if self.analysis_state == "loading":
            self.video_title.setText(self.tr("reading_info"))
            self.video_uploader.setText(self.tr("uploader_empty"))
            self.video_details.setText(self.tr("platform_empty"))
        elif self.analysis_state == "success":
            self._render_analysis_metadata()
        elif self.analysis_state == "error":
            self.video_title.setText(self.tr("could_not_analyze"))
            self.analysis_status.setText(
                translate_message(self.language, self.analysis_error_message)
            )
        else:
            self.video_title.setText(self.tr("preview_hint"))
            self.video_uploader.setText(self.tr("uploader_empty"))
            self.video_details.setText(self.tr("platform_empty"))

        thumbnail_texts = {
            "empty": "no_preview",
            "loading": "loading",
            "no_thumbnail": "no_thumbnail",
            "unavailable": "thumbnail_unavailable",
        }
        thumbnail_key = thumbnail_texts.get(self.thumbnail_state)
        if thumbnail_key:
            self.thumbnail_label.setText(self.tr(thumbnail_key))

        self._refresh_tool_status()
        for widget in self.job_widgets.values():
            widget.set_language(self.language)

    def _restore_settings(self) -> None:
        default_downloads = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        if not default_downloads:
            default_downloads = str(Path.home() / "Downloads")
        saved_path = self.settings.value("save_dir", default_downloads, str)
        self.save_path.setText(saved_path)
        self._set_combo_data(
            self.video_quality, self.settings.value("video_quality", "1080p", str)
        )
        self._set_combo_data(
            self.audio_format, self.settings.value("audio_format", "MP3", str)
        )
        self._set_combo_data(
            self.audio_quality, self.settings.value("audio_quality", "Best", str)
        )
        mode = self.settings.value("mode", "video", str)
        self.audio_radio.setChecked(mode == "audio")
        self.video_radio.setChecked(mode != "audio")
        self.browser_cookies.setChecked(
            self.settings.value("use_browser_cookies", False, bool)
        )
        self.browser_combo.setCurrentText(self.settings.value("browser", "Chrome", str))
        cookie_file = self.settings.value("cookie_file", "", str)
        if cookie_file and Path(cookie_file).is_file():
            self.cookie_file_label.setText(cookie_file)
        else:
            self.settings.setValue("cookie_file", "")

    def _save_settings(self) -> None:
        self.settings.setValue("save_dir", self.save_path.text())
        self.settings.setValue("video_quality", self.video_quality.currentData())
        self.settings.setValue("audio_format", self.audio_format.currentData())
        self.settings.setValue("audio_quality", self.audio_quality.currentData())
        self.settings.setValue("mode", "video" if self.video_radio.isChecked() else "audio")
        self.settings.setValue("use_browser_cookies", self.browser_cookies.isChecked())
        self.settings.setValue("browser", self.browser_combo.currentText())
        self.settings.setValue("language", self.language)

    def _refresh_tool_status(self) -> None:
        ytdlp_text = "yt-dlp ✓" if self.ytdlp.available else self.tr("ytdlp_missing")
        ffmpeg_text = "FFmpeg ✓" if self.ffmpeg.available else self.tr("ffmpeg_missing_short")
        self.tool_status.setText(f"{ytdlp_text}   •   {ffmpeg_text}")
        self.tool_status.setToolTip(
            f"yt-dlp: {self.ytdlp.display_path or self.tr('not_found')}\n"
            f"FFmpeg: {self.ffmpeg.path or self.tr('not_found')}"
        )

    def paste_clipboard(self) -> None:
        text = QApplication.clipboard().text().strip()
        if text:
            self.url_input.setPlainText(text)

    def browse_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, self.tr("select_download_folder"), self.save_path.text()
        )
        if selected:
            self.save_path.setText(selected)
            self.settings.setValue("save_dir", selected)

    def import_cookies(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("import_cookies_title"),
            str(Path.home()),
            self.tr("cookies_filter"),
        )
        if selected:
            self.settings.setValue("cookie_file", selected)
            self.cookie_file_label.setText(selected)

    def clear_cookies(self) -> None:
        self.settings.setValue("cookie_file", "")
        self.cookie_file_label.setText(self.tr("no_cookie_file"))

    def _cookie_file(self) -> str:
        value = self.settings.value("cookie_file", "", str)
        return value if value and Path(value).is_file() else ""

    def analyze_url(self) -> None:
        urls = extract_urls(self.url_input.toPlainText())
        if not urls:
            self._show_analysis_error("Enter a valid http:// or https:// video URL.", "")
            return
        if self.analyze_thread and self.analyze_thread.isRunning():
            return
        self.analysis_status.hide()
        self.analysis_details_button.hide()
        self.analysis_error_details = ""
        self.analysis_error_message = ""
        self.analysis_state = "loading"
        self.thumbnail_state = "loading"
        self.analyze_button.setEnabled(False)
        self.analyze_button.setText(self.tr("analyzing"))
        self.video_title.setText(self.tr("reading_info"))
        self.video_uploader.setText(self.tr("uploader_empty"))
        self.video_details.setText(self.tr("platform_empty"))
        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText(self.tr("loading"))

        self.analyzed_url = urls[0]
        thread = QThread(self)
        worker = AnalyzeWorker(
            self.ytdlp,
            urls[0],
            self.browser_cookies.isChecked(),
            self.browser_combo.currentText(),
            self._cookie_file(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._analysis_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_done)
        thread.finished.connect(thread.deleteLater)
        self.analyze_thread = thread
        self.analyze_worker = worker
        thread.start()

    def _analysis_finished(
        self, success: bool, metadata: object, message: str, details: str
    ) -> None:
        self.analyze_button.setEnabled(True)
        self.analyze_button.setText(self.tr("analyze"))
        if not success or not isinstance(metadata, dict):
            self.analysis_metadata = {}
            self.analysis_state = "error"
            self.thumbnail_state = "empty"
            self.video_title.setText(self.tr("could_not_analyze"))
            self.thumbnail_label.setText(self.tr("no_preview"))
            self._show_analysis_error(message, details)
            return
        self.analysis_metadata = metadata
        self.analysis_state = "success"
        self._render_analysis_metadata()
        thumbnail = str(metadata.get("thumbnail") or "")
        if thumbnail:
            self.thumbnail_state = "loading"
            self.thumbnail_label.setText(self.tr("loading"))
            self._load_thumbnail(thumbnail)
        else:
            self.thumbnail_state = "no_thumbnail"
            self.thumbnail_label.setText(self.tr("no_thumbnail"))

    def _render_analysis_metadata(self) -> None:
        metadata = self.analysis_metadata
        title = str(metadata.get("title") or self.tr("untitled_video"))
        uploader = str(
            metadata.get("uploader") or metadata.get("channel") or self.tr("unknown")
        )
        platform = str(
            metadata.get("extractor_key")
            or metadata.get("extractor")
            or metadata.get("webpage_url_domain")
            or self.tr("website")
        )
        self.video_title.setText(title)
        self.video_title.setToolTip(title)
        self.video_uploader.setText(self.tr("uploader", name=uploader))
        self.video_details.setText(
            self.tr(
                "platform_duration",
                platform=platform,
                duration=format_duration(metadata.get("duration")),
            )
        )

    def _analysis_thread_done(self) -> None:
        self.analyze_thread = None
        self.analyze_worker = None

    def _show_analysis_error(self, message: str, details: str) -> None:
        self.analysis_state = "error"
        self.thumbnail_state = "empty"
        self.analysis_error_message = message
        self.analysis_error_details = details
        self.video_title.setText(self.tr("could_not_analyze"))
        self.video_uploader.setText(self.tr("uploader_empty"))
        self.video_details.setText(self.tr("platform_empty"))
        self.thumbnail_label.setPixmap(QPixmap())
        self.thumbnail_label.setText(self.tr("no_preview"))
        self.analysis_status.setText(translate_message(self.language, message))
        self.analysis_status.setToolTip(details)
        self.analysis_status.show()
        self.analysis_details_button.setVisible(bool(details))
        if details:
            logger.warning("Analysis error: %s", message)

    def show_analysis_details(self) -> None:
        if self.analysis_error_details:
            self._message(
                self.tr("analysis_details"),
                self.analysis_status.text(),
                self.analysis_error_details,
            )

    def _load_thumbnail(self, url: str) -> None:
        if self.thumbnail_reply:
            self.thumbnail_reply.abort()
            self.thumbnail_reply.deleteLater()
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", b"Mozilla/5.0")
        reply = self.network.get(request)
        self.thumbnail_reply = reply
        reply.finished.connect(lambda current=reply: self._thumbnail_finished(current))

    def _thumbnail_finished(self, reply: QNetworkReply) -> None:
        if reply is not self.thumbnail_reply:
            reply.deleteLater()
            return
        self.thumbnail_reply = None
        data = bytes(reply.readAll())
        pixmap = QPixmap()
        if (
            reply.error() == QNetworkReply.NetworkError.NoError
            and pixmap.loadFromData(data)
        ):
            self.thumbnail_state = "loaded"
            self.thumbnail_label.setText("")
            self.thumbnail_label.setPixmap(
                pixmap.scaled(
                    self.thumbnail_label.size(),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
            )
        else:
            self.thumbnail_state = "unavailable"
            self.thumbnail_label.setText(self.tr("thumbnail_unavailable"))
        reply.deleteLater()

    def queue_downloads(self) -> None:
        urls = extract_urls(self.url_input.toPlainText())
        if not urls:
            self._message(self.tr("invalid_url_title"), self.tr("invalid_url"))
            return
        destination = Path(self.save_path.text())
        try:
            destination.mkdir(parents=True, exist_ok=True)
            test_target = destination / f".video_downloader_write_test_{os.getpid()}"
            test_target.touch(exist_ok=True)
            test_target.unlink(missing_ok=True)
        except OSError as exc:
            self._message(
                self.tr("folder_unavailable_title"),
                self.tr("folder_unavailable"),
                f"{type(exc).__name__}: {exc}",
            )
            return

        self._save_settings()
        mode = "video" if self.video_radio.isChecked() else "audio"
        for url in urls:
            parsed = urlparse(url)
            host = parsed.netloc.removeprefix("www.") or self.tr("website")
            metadata_matches = bool(self.analysis_metadata) and url == self.analyzed_url
            metadata_title = self.analysis_metadata.get("title") if metadata_matches else None
            title = (
                str(metadata_title) if metadata_title else host
            )
            platform = (
                str(
                    self.analysis_metadata.get("extractor_key")
                    or self.analysis_metadata.get("extractor")
                    or host
                )
                if metadata_matches
                else host
            )
            job = DownloadJob(
                url=url,
                title=title,
                platform=platform,
                mode=mode,
                video_quality=str(self.video_quality.currentData()),
                audio_format=str(self.audio_format.currentData()),
                audio_quality=str(self.audio_quality.currentData()),
                save_dir=str(destination),
                use_browser_cookies=self.browser_cookies.isChecked(),
                browser=self.browser_combo.currentText(),
                cookie_file=self._cookie_file(),
                title_pending=not bool(metadata_title),
            )
            self.manager.add(job)

    def on_job_changed(self, job_id: str, job_object: object) -> None:
        if not isinstance(job_object, DownloadJob):
            return
        widget = self.job_widgets.get(job_id)
        if not widget:
            self.empty_queue.hide()
            widget = DownloadJobWidget(job_object, self.language)
            widget.cancel_requested.connect(self.manager.cancel)
            widget.retry_requested.connect(self.manager.retry)
            widget.remove_requested.connect(self.manager.remove)
            widget.open_requested.connect(self.open_job_folder)
            widget.details_requested.connect(self.show_job_details)
            self.job_widgets[job_id] = widget
            self.queue_layout.addWidget(widget)
        widget.update_job(job_object)

    def on_job_removed(self, job_id: str) -> None:
        widget = self.job_widgets.pop(job_id, None)
        if widget:
            self.queue_layout.removeWidget(widget)
            widget.deleteLater()
        self.empty_queue.setVisible(not self.job_widgets)

    def open_job_folder(self, job_id: str) -> None:
        job = self.manager.jobs.get(job_id)
        if not job:
            return
        target = Path(job.output_path).parent if job.output_path else Path(job.save_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def show_job_details(self, job_id: str) -> None:
        job = self.manager.jobs.get(job_id)
        if not job:
            return
        self._message(
            translate_status(self.language, job.status),
            translate_message(self.language, job.error)
            or self.tr("status_line", status=translate_status(self.language, job.status)),
            job.details or self.tr("no_technical_details"),
        )

    def _message(self, title: str, text: str, details: str = "") -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Warning)
        box.setText(text)
        if details:
            box.setDetailedText(details)
        box.exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.manager.has_active_jobs():
            answer = QMessageBox.question(
                self,
                self.tr("exit_title"),
                self.tr("exit_question"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._save_settings()
        if self.thumbnail_reply:
            self.thumbnail_reply.abort()
        if self.analyze_worker:
            self.analyze_worker.cancel()
        if self.analyze_thread and self.analyze_thread.isRunning():
            self.analyze_thread.quit()
            self.analyze_thread.wait(5000)
        self.manager.shutdown()
        logger.info("Application stopped")
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
