"""Storage manager: one directory per job, dedupe cache, TTL cleanup.

Per-job directories exist because the previous generation of these bots wrote to a
fixed ``downloaded_video.mp4`` path, so two concurrent users overwrote each other.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .security import sanitize_filename

log = logging.getLogger("grabber.storage")

MEDIA_SUFFIXES = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts",
    ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif", ".bmp", ".tiff",
    ".zip", ".pdf", ".epub",
}


@dataclass(slots=True)
class CachedFile:
    path: Path
    size_bytes: int
    job_id: str


def cache_key(url: str, engine: str, variant: str) -> str:
    payload = f"{engine}|{variant}|{url.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class StorageManager:
    def __init__(self, settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._reserved: set[Path] = set()
        for directory in (settings.data_dir, settings.downloads_dir, settings.cache_dir,
                          settings.cookies_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ---------------- paths ----------------

    def job_dir(self, job_id: str) -> Path:
        path = self.settings.downloads_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def reserve(self, job_id: str, suggested_name: str) -> Path:
        """Unique, traversal-free destination inside the job directory.

        Uniqueness is tracked in memory as well as on disk: two reserves can both happen
        before either engine writes anything, and handing them the same path is exactly
        the overwrite race this layout exists to prevent.
        """
        name = sanitize_filename(suggested_name, fallback=job_id)
        candidate = self.job_dir(job_id) / name
        if not candidate.exists() and candidate not in self._reserved:
            self._reserved.add(candidate)
            return candidate
        stem, suffix = candidate.stem[:100], candidate.suffix
        for index in range(1, 1000):
            alternate = candidate.with_name(f"{stem}-{index}{suffix}")
            if not alternate.exists() and alternate not in self._reserved:
                self._reserved.add(alternate)
                return alternate
        fallback = candidate.with_name(f"{stem}-{int(time.time() * 1000)}{suffix}")
        self._reserved.add(fallback)
        return fallback

    def final_outputs(self, job_id: str) -> list[Path]:
        directory = self.job_dir(job_id)
        files = [p for p in directory.rglob("*") if p.is_file()]
        media = [p for p in files if p.suffix.lower() in MEDIA_SUFFIXES]
        ordered = sorted(media or files, key=lambda p: p.stat().st_mtime, reverse=True)
        return ordered

    def largest_output(self, job_id: str) -> Path | None:
        outputs = self.final_outputs(job_id)
        if not outputs:
            return None
        return max(outputs, key=lambda p: p.stat().st_size)

    def delete_job_files(self, job_id: str) -> bool:
        directory = self.settings.downloads_dir / job_id
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            return True
        return False

    # ---------------- dedupe ----------------

    def lookup_cached(self, url: str, engine: str, variant: str) -> CachedFile | None:
        hit = self.db.dedupe_lookup(cache_key(url, engine, variant))
        if not hit:
            return None
        job_id, file_path = hit
        if not file_path:
            return None
        path = Path(file_path)
        if not path.is_file():
            return None
        return CachedFile(path=path, size_bytes=path.stat().st_size, job_id=job_id)

    def store_cached(self, url: str, engine: str, variant: str, *, job_id: str,
                     file_path: Path | None) -> None:
        self.db.dedupe_store(
            cache_key(url, engine, variant),
            job_id=job_id,
            file_path=str(file_path) if file_path else None,
            ttl_seconds=self.settings.dedupe_ttl_minutes * 60,
        )

    # ---------------- maintenance ----------------

    def purge_expired(self) -> dict[str, int]:
        """Remove job directories past their TTL and forget finished job rows."""
        stats = {"dirs_removed": 0, "bytes_freed": 0, "jobs_forget": 0}
        cutoff = time.time() - self.settings.temp_ttl_minutes * 60
        root = self.settings.downloads_dir
        if root.is_dir():
            for directory in root.iterdir():
                if not directory.is_dir():
                    continue
                try:
                    newest = max(
                        (item.stat().st_mtime for item in directory.rglob("*") if item.is_file()),
                        default=directory.stat().st_mtime,
                    )
                except OSError:
                    continue
                if newest >= cutoff:
                    continue
                size = sum(
                    item.stat().st_size for item in directory.rglob("*") if item.is_file()
                )
                shutil.rmtree(directory, ignore_errors=True)
                stats["dirs_removed"] += 1
                stats["bytes_freed"] += size
        stats["jobs_forget"] = self.db.forget_old(older_than_seconds=self.settings.temp_ttl_minutes * 60)
        return stats

    def used_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.settings.downloads_dir.rglob("*")
            if path.is_file()
        )


__all__ = ["StorageManager", "CachedFile", "cache_key", "MEDIA_SUFFIXES"]
