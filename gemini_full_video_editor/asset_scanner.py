from __future__ import annotations

from pathlib import Path
from typing import Callable

from .media_utils import probe_media
from .models import Asset


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}


def scan_assets(folder: Path | None, logger: Callable[[str], None]) -> list[Asset]:
    if folder is None:
        return []
    if not folder.is_dir():
        raise ValueError("Assets folder does not exist.")
    files = sorted(
        (item for item in folder.iterdir() if item.is_file()),
        key=lambda item: item.name.casefold(),
    )
    assets: list[Asset] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            continue
        try:
            info = probe_media(path)
        except Exception as exc:
            logger(f"Skipping asset {path.name}: {exc}")
            continue
        asset_type = "image" if suffix in IMAGE_EXTENSIONS else "video"
        assets.append(
            Asset(
                id=f"asset_{len(assets) + 1:03d}",
                filename=path.name,
                path=path.resolve(),
                type=asset_type,
                duration=info.duration if asset_type == "video" else None,
                width=info.width,
                height=info.height,
            )
        )
    return assets
