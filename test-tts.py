"""Liệt kê các giọng Microsoft Edge TTS, được nhóm theo quốc gia.

Chạy:
    python -m pip install -U edge-tts
    python test-tts.py

Ví dụ lọc Việt Nam:
    python test-tts.py --country VN
    python test-tts.py --country Vietnam
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def country_from_voice(voice: dict[str, Any]) -> tuple[str, str]:
    """Trả về mã và tên quốc gia từ dữ liệu voice do edge-tts cung cấp."""
    locale = str(voice.get("Locale", ""))
    parts = locale.rsplit("-", 1)
    country_code = parts[-1].upper() if len(parts) == 2 else "UNKNOWN"

    # `FriendlyName` thường có dạng "Microsoft <tên> Online (Natural) - <ngôn ngữ> (<quốc gia>)".
    friendly_name = str(voice.get("FriendlyName", ""))
    if "(" in friendly_name and friendly_name.endswith(")"):
        country_name = friendly_name.rsplit("(", 1)[-1][:-1].strip()
    else:
        country_name = country_code

    return country_code, country_name


def matches_country(
    country_filter: str | None, country_code: str, country_name: str
) -> bool:
    if not country_filter:
        return True

    needle = country_filter.casefold()
    return needle in country_code.casefold() or needle in country_name.casefold()


async def get_voices() -> list[dict[str, Any]]:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài edge-tts. Chạy: python -m pip install -U edge-tts"
        ) from exc

    return await edge_tts.list_voices()


async def main() -> int:
    # Windows PowerShell/Command Prompt đôi khi dùng code page không in được tiếng Việt.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Liệt kê Edge TTS voices theo quốc gia (dữ liệu trực tiếp từ Microsoft)."
    )
    parser.add_argument(
        "--country",
        "-c",
        help="Lọc theo mã hoặc tên quốc gia, ví dụ: VN, US, Vietnam, United States.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("edge_tts_voices_by_country.json"),
        help="File JSON đầu ra (mặc định: edge_tts_voices_by_country.json).",
    )
    args = parser.parse_args()

    try:
        voices = await get_voices()
    except RuntimeError as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Không lấy được danh sách Edge TTS voices: {exc}", file=sys.stderr)
        return 1

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for voice in voices:
        country_code, country_name = country_from_voice(voice)
        if matches_country(args.country, country_code, country_name):
            grouped[(country_code, country_name)].append(voice)

    if not grouped:
        print(f"Không tìm thấy voice cho quốc gia: {args.country}")
        return 0

    json_data = {
        "total_voices": sum(len(country_voices) for country_voices in grouped.values()),
        "country_filter": args.country,
        "countries": [
            {
                "country_code": country_code,
                "country_name": country_name,
                "voices": sorted(
                    country_voices, key=lambda item: str(item.get("ShortName", ""))
                ),
            }
            for (country_code, country_name), country_voices in sorted(
                grouped.items(), key=lambda item: (item[0][1], item[0][0])
            )
        ],
    }
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"Không thể ghi file JSON {args.output}: {exc}", file=sys.stderr)
        return 1

    total = 0
    for (country_code, country_name), country_voices in sorted(
        grouped.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        print(f"\n{'=' * 72}\n{country_name} ({country_code}) — {len(country_voices)} voices")
        for voice in sorted(country_voices, key=lambda item: str(item.get("ShortName", ""))):
            print(
                "  "
                f"{voice.get('ShortName', 'N/A'):<35} "
                f"| {voice.get('Gender', 'N/A'):<6} "
                f"| {voice.get('Locale', 'N/A')}"
            )
            total += 1

    print(f"\nTổng cộng: {total}/{len(voices)} voices.")
    print(f"Đã ghi JSON: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
