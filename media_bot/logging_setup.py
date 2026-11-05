"""Logging setup: rotating file + console, with tokens scrubbed."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_SECRET_RE = re.compile(r"(?i)(token|api[_-]?hash|secret|key|password|cookie)([=:\"' ]+)([^\s,\"']{6,})")


def _output_streams() -> list[Any]:
    import sys

    return [stream for stream in (sys.stdout, sys.stderr) if stream is not None]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return _SECRET_RE.sub(r"\1\2***", text)


def setup_logging(data_dir: Path | str, level: str = "INFO") -> logging.Logger:
    # Windows consoles default to a legacy codepage; Arabic captions would raise
    # UnicodeEncodeError there, so force UTF-8 with a replacement policy.
    for stream in _output_streams():
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # pragma: no cover - redirected stdout
            pass

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        directory / "grabber.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # aiohttp dumps whole request bodies at INFO; keep them at WARNING.
    for noisy in ("aiogram.client", "aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("grabber")


__all__ = ["setup_logging", "RedactingFormatter"]
