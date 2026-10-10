"""TTS For Free "SRT to speech" client.

API (https://api.ttsforfree.com/swagger/index.html):
    POST /api/TTS/CreateSrt             submit subtitle blocks -> job id
    GET  /api/TTS/Multi/Status/{jobid}  poll every ~5s until Success/Completed

The service renders one audio file in which every block is placed at its
StartMs/EndMs and sped up/down inside MinSpeechRate..MaxSpeechRate to fit.

Install:
    pip install -U requests cryptography
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from .models import Cue

try:
    import requests
except ImportError:  # pragma: no cover - reported when the client is built
    requests = None  # type: ignore[assignment]

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - reported when a plain voice is encrypted
    AESGCM = None  # type: ignore[assignment]


LogFn = Callable[[str], None]

DEFAULT_ENDPOINT = "https://api.ttsforfree.com"
CREATE_SRT_PATH = "/api/TTS/CreateSrt"
STATUS_PATH_TEMPLATE = "/api/TTS/Multi/Status/{job_id}"
VOICE_CRYPTO_KEY_B64 = "K1hXK1B4K0pMaHBDaHh6WXh1R2J4S3ZBbkJoQ2l4eWw="
REQUEST_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 20 * 60

# Documented limits of the auto-fit speech-speed range.
MIN_SPEECH_RATE = 0.80
MAX_SPEECH_RATE = 2.50
DEFAULT_MIN_SPEECH_RATE = 0.95
DEFAULT_MAX_SPEECH_RATE = 1.50
DEFAULT_BLOCK_SPEED = 0.9


class TtsForFreeError(RuntimeError):
    """The service rejected or failed the job."""


def ensure_dependencies() -> None:
    if requests is None:
        raise RuntimeError("Thiếu thư viện requests. Chạy: python -m pip install -U requests")


def ci_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Case-insensitive lookup: the API answers with PascalCase keys."""
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def encrypt_voice_token(voice: str) -> str:
    """Return the `v1:` token the backend expects (plain voice ids are encrypted)."""
    token = (voice or "").strip()
    if not token:
        raise RuntimeError("Chưa nhập giọng (voice).")
    if token.startswith("v1:"):
        return token
    if AESGCM is None:
        raise RuntimeError(
            "Thiếu thư viện cryptography để mã hóa voice. "
            "Chạy: python -m pip install -U cryptography (hoặc dán voice token bắt đầu bằng v1:)."
        )
    nonce = os.urandom(12)
    ciphertext_with_tag = AESGCM(base64.b64decode(VOICE_CRYPTO_KEY_B64)).encrypt(
        nonce, token.encode("utf-8"), None
    )
    cipher, tag = ciphertext_with_tag[:-16], ciphertext_with_tag[-16:]
    encoded = base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii").rstrip("=")
    return "v1:" + encoded


def normalize_endpoint(endpoint: str) -> str:
    """Accept the site root, the swagger/docs URL or the full CreateSrt URL."""
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        raise RuntimeError("Chưa nhập TTS endpoint.")
    lowered = value.lower()
    for suffix in ("/swagger/index.html", "/swagger", "/docs", CREATE_SRT_PATH):
        if lowered.endswith(suffix.lower()):
            value = value[: -len(suffix)]
            lowered = value.lower()
    return value.rstrip("/")


def guess_extension(url: str, default: str = ".mp3") -> str:
    match = re.search(r"\.([a-zA-Z0-9]{2,5})(?:$|\?)", url or "")
    return "." + match.group(1).lower() if match else default


def clamp_speech_rates(min_rate: float, max_rate: float) -> tuple[float, float]:
    low = min(max(float(min_rate), MIN_SPEECH_RATE), MAX_SPEECH_RATE)
    high = min(max(float(max_rate), MIN_SPEECH_RATE), MAX_SPEECH_RATE)
    return (low, high) if low <= high else (high, low)


class TtsForFreeSrtClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        logger: LogFn | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        ensure_dependencies()
        self.base_url = normalize_endpoint(endpoint)
        self.api_key = (api_key or "").strip()
        self.logger = logger or (lambda _message: None)
        self.cancel_event = cancel_event
        self.session = requests.Session()
        self.connection_id = str(uuid.uuid4())

    # ---------- public ----------
    def synthesize(
        self,
        cues: Sequence[Cue],
        voice: str,
        output_path: Path,
        *,
        speed: float = DEFAULT_BLOCK_SPEED,
        pitch: float = 0.0,
        volume: float = 1.0,
        min_rate: float = DEFAULT_MIN_SPEECH_RATE,
        max_rate: float = DEFAULT_MAX_SPEECH_RATE,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: float = POLL_TIMEOUT_SECONDS,
    ) -> Path:
        """Submit the cues, wait for the job and download the audio. Returns the file path."""
        job_id = self.submit(
            cues, voice, speed=speed, pitch=pitch, volume=volume, min_rate=min_rate, max_rate=max_rate
        )
        audio_url = self.poll(job_id, poll_interval=poll_interval, timeout=timeout)
        final_path = output_path.with_suffix(guess_extension(audio_url))
        return self.download(audio_url, final_path)

    def submit(
        self,
        cues: Sequence[Cue],
        voice: str,
        *,
        speed: float = DEFAULT_BLOCK_SPEED,
        pitch: float = 0.0,
        volume: float = 1.0,
        min_rate: float = DEFAULT_MIN_SPEECH_RATE,
        max_rate: float = DEFAULT_MAX_SPEECH_RATE,
    ) -> int:
        if not cues:
            raise RuntimeError("SRT không có cue hợp lệ để đọc.")
        token = encrypt_voice_token(voice)
        low, high = clamp_speech_rates(min_rate, max_rate)
        payload = {
            "connectionId": self.connection_id,
            "minSpeechRate": low,
            "maxSpeechRate": high,
            "blocks": [
                {
                    "texts": cue.text,
                    "voice": token,
                    "speed": speed,
                    "pitch": pitch,
                    "volume": volume,
                    "startMs": int(round(cue.start * 1000)),
                    "endMs": int(round(cue.end * 1000)),
                }
                for cue in cues
            ],
        }
        self.logger(f"Gửi {len(cues)} đoạn lên TTS For Free (CreateSrt)...")
        response = self.session.post(
            self.base_url + CREATE_SRT_PATH,
            json=payload,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = self._parse(response)
        job_id = ci_get(data, "jobId", "id")
        if not isinstance(job_id, (int, float, str)):
            raise TtsForFreeError("CreateSrt không trả về job id.")
        self.logger(f"Đã tạo job TTS #{job_id}.")
        return int(job_id)

    def poll(
        self,
        job_id: int,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: float = POLL_TIMEOUT_SECONDS,
    ) -> str:
        status_url = self.base_url + STATUS_PATH_TEMPLATE.format(job_id=job_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            response = self.session.get(
                status_url, headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS
            )
            data = self._parse(response)
            status = str(ci_get(data, "status", default="")).strip().lower()
            audio_url = ci_get(data, "data", "audioUrl", "url", "expectUrl")
            if status in {"success", "completed"}:
                if not audio_url:
                    raise TtsForFreeError(f"Job #{job_id} xong nhưng không có link audio.")
                self.logger(f"Job TTS #{job_id} hoàn tất.")
                return str(audio_url)
            self.logger(f"Job TTS #{job_id}: {status or 'pending'}...")
            self._sleep(poll_interval)
        raise TimeoutError(f"Job TTS #{job_id} chưa xong sau {int(timeout)} giây.")

    def download(self, audio_url: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = output_path.with_name(output_path.name + ".part")
        self.logger("Đang tải audio giọng...")
        try:
            with self.session.get(
                audio_url, headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS, stream=True
            ) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        self._check_cancelled()
                        if chunk:
                            handle.write(chunk)
            if partial.stat().st_size == 0:
                raise TtsForFreeError("File audio tải về rỗng.")
            partial.replace(output_path)
        finally:
            partial.unlink(missing_ok=True)
        return output_path

    # ---------- internals ----------
    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "GeminiTtsForFreeTool/1.0",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        bearer = os.getenv("TTSFORFREE_BEARER_TOKEN", "").strip()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        cookie = os.getenv("TTSFORFREE_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        extra = os.getenv("TTSFORFREE_EXTRA_HEADERS", "").strip()
        if extra:
            try:
                parsed = json.loads(extra)
            except json.JSONDecodeError as exc:
                raise RuntimeError("TTSFORFREE_EXTRA_HEADERS phải là JSON hợp lệ.") from exc
            if not isinstance(parsed, dict):
                raise RuntimeError("TTSFORFREE_EXTRA_HEADERS phải là JSON object.")
            headers.update({str(key): str(value) for key, value in parsed.items()})
        return headers

    def _parse(self, response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            detail = ci_get(data, "message") if isinstance(data, dict) else None
            if response.status_code in (401, 403):
                detail = detail or "API key không hợp lệ hoặc không có quyền."
            raise TtsForFreeError(
                f"TTS For Free trả lỗi HTTP {response.status_code}: "
                f"{detail or response.text[:200].strip() or 'không rõ'}"
            )
        if not isinstance(data, dict):
            raise TtsForFreeError("TTS For Free trả về dữ liệu không đúng định dạng.")
        status = str(ci_get(data, "status", default="")).strip().lower()
        job_id = ci_get(data, "id", "jobId")
        if status in {"error", "failed"} or (isinstance(job_id, (int, float)) and job_id == -1):
            raise TtsForFreeError(str(ci_get(data, "message", default="TTS For Free báo lỗi job.")))
        return data

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InterruptedError("Stopped by user.")

    def _sleep(self, seconds: float) -> None:
        if self.cancel_event is not None:
            if self.cancel_event.wait(seconds):
                raise InterruptedError("Stopped by user.")
        else:
            time.sleep(seconds)
