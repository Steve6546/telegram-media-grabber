"""Shared data models for extraction and downloading.

A :class:`Bundle` is what the bot shows the user after analysis; a :class:`Variant`
is one honest, downloadable choice inside that bundle. Nothing here stores a quality
that the engine did not actually report.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    ALBUM = "album"
    FILE = "file"
    LIVE = "live"
    MIXED = "mixed"


class Want(str, Enum):
    """What the user asked for; drives engine preference."""

    AUTO = "auto"
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"


ProgressHook = Callable[[float, str | None], Awaitable[None]]


@dataclass(slots=True)
class Variant:
    token: str
    label: str
    kind: MediaKind
    format_spec: str
    ext: str
    height: int | None = None
    fps: float | None = None
    vbr: float | None = None
    abr: float | None = None
    filesize: int | None = None
    merge: bool = False
    codec: str | None = None
    note: str = ""

    def size_text(self) -> str:
        if not self.filesize:
            return "حجم غير معروف"
        return human_size(self.filesize)


@dataclass(slots=True)
class MediaItem:
    url: str
    kind: MediaKind
    name: str | None = None
    width: int | None = None
    height: int | None = None
    filesize: int | None = None
    ext: str | None = None
    source: str | None = None
    index: int | None = None


@dataclass(slots=True)
class Bundle:
    engine: str
    platform: str
    url: str
    kind: MediaKind
    token: str = ""
    title: str | None = None
    thumbnail: str | None = None
    duration: int | None = None
    uploader: str | None = None
    webpage_url: str | None = None
    variants: list[Variant] = field(default_factory=list)
    items: list[MediaItem] = field(default_factory=list)
    total_bytes: int | None = None
    count: int = 1
    needs_auth: bool = False
    has_drm: bool = False
    live: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_multi(self) -> bool:
        return self.count > 1 or len(self.items) > 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        for variant in data["variants"]:
            variant["kind"] = variant["kind"].value if hasattr(variant["kind"], "value") else variant["kind"]
        for item in data["items"]:
            item["kind"] = item["kind"].value if hasattr(item["kind"], "value") else item["kind"]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Bundle:
        variants = [
            Variant(**{**variant, "kind": MediaKind(variant["kind"])})
            for variant in data.get("variants", [])
        ]
        items = [
            MediaItem(**{**item, "kind": MediaKind(item["kind"])}) for item in data.get("items", [])
        ]
        payload = {key: value for key, value in data.items() if key not in ("variants", "items", "kind")}
        return cls(**payload, kind=MediaKind(data["kind"]), variants=variants, items=items)


@dataclass(slots=True)
class DownloadResult:
    files: list[Path]
    bytes_total: int
    engine: str
    merged: bool = False
    remuxed: bool = False
    partial: bool = False
    #: what ffprobe actually found in the delivered file, e.g. "1920x1080 · h264 · 128 kbps"
    info: str | None = None

    @property
    def main_file(self) -> Path:
        return max(self.files, key=lambda path: path.stat().st_size)


def human_size(num_bytes: float | None) -> str:
    if num_bytes is None:
        return "غير معروف"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - unreachable


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return "غير معروف"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class Extractor(abc.ABC):
    """One engine. Add a platform by teaching an engine about it, not by editing the bot."""

    engine_id: str = "base"
    #: kinds this engine can produce, used for routing when several engines match
    provides: frozenset[MediaKind] = frozenset()
    #: lower runs first when several engines claim the same URL
    priority: int = 100

    def __init__(self, settings) -> None:
        self.settings = settings

    @abc.abstractmethod
    def supports(self, host: str, url: str, *, want: Want = Want.AUTO) -> bool:
        """Whether this engine should handle the URL."""

    @abc.abstractmethod
    async def extract(self, url: str, *, host: str = "", cookies_path: Path | None = None,
                      want: Want = Want.AUTO) -> Bundle:
        """Analyse ``url`` and return the honest set of choices."""

    @abc.abstractmethod
    async def download(self, bundle: Bundle, variant: Variant | None, dest_dir: Path, *,
                       progress: ProgressHook | None = None,
                       cookies_path: Path | None = None) -> DownloadResult:
        """Fetch the chosen variant into ``dest_dir``."""

    # helpers shared by engines
    def new_bundle(self, url: str, *, platform: str, kind: MediaKind) -> Bundle:
        return Bundle(engine=self.engine_id, platform=platform, url=url, kind=kind)


__all__ = [
    "MediaKind", "Want", "Variant", "MediaItem", "Bundle", "DownloadResult", "Extractor",
    "ProgressHook", "human_size", "human_duration",
]
