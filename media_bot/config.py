"""Runtime configuration, loaded from environment with an optional YAML file.

Nothing here silently defaults to a value that would make the bot lie to the user:
send-mode and engine availability are probed later by :mod:`media_bot.cli` (``doctor``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FILE = "config.yaml"

#: Many hosts (Wikimedia in particular) refuse anonymous browser-spoofing clients and ask
#: for a self-identifying one. Sending the truth also makes our traffic debuggable.
USER_AGENT = "telegram-media-grabber/0.1 (self-hosted Telegram bot; +https://github.com/Steve6546/telegram-media-grabber)"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfig surfaces at startup
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when configuration is unusable."""


@dataclass(slots=True)
class Settings:
    # --- Telegram ---
    bot_token: str = ""
    # "bot_api" (50 MB ceiling), "local_api" (2 GB via telegram-bot-api server),
    # "mtproto" (2 GB via a Telethon user session). "auto" picks the strongest available.
    send_mode: str = "auto"
    local_api_url: str = "http://127.0.0.1:8081"
    #: when the Local Bot API Server runs elsewhere, map its root to the bot's root
    #: so a path the bot writes is the path the API server reads.
    local_files_root: str = ""
    bot_files_root: str = ""
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_name: str = "grabber-mtproto"

    # --- Storage / limits ---
    data_dir: Path = Path("data")
    max_concurrent_downloads: int = 3
    max_file_mb: int = 2000  # hard stop; sender ceilings are enforced separately
    per_user_concurrent: int = 1
    temp_ttl_minutes: int = 90
    dedupe_ttl_minutes: int = 60
    progress_interval_seconds: float = 2.0
    request_timeout_seconds: int = 45
    read_timeout_seconds: int = 600

    # --- Downloads ---
    use_aria2: bool = True
    aria2c_path: str = "aria2c"
    aria2_connections: int = 8
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    resume: bool = True

    # --- Private content ---
    allow_cookie_auth: bool = True
    cookie_encryption_key: str = ""

    # --- Access control ---
    allowed_user_ids: tuple[int, ...] = ()
    max_downloads_per_user_per_hour: int = 30

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def cookies_dir(self) -> Path:
        return self.data_dir / "cookies"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "grabber.sqlite3"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def mtproto_configured(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)

    def validate(self) -> None:
        problems: list[str] = []
        if self.send_mode not in {"auto", "bot_api", "local_api", "mtproto"}:
            problems.append(f"send_mode must be auto|bot_api|local_api|mtproto, got {self.send_mode!r}")
        if self.max_concurrent_downloads < 1:
            problems.append("max_concurrent_downloads must be >= 1")
        if self.per_user_concurrent < 1:
            problems.append("per_user_concurrent must be >= 1")
        if not 1 <= self.max_file_mb <= 4096:
            problems.append("max_file_mb must be between 1 and 4096 (Telegram MTProto ceiling)")
        if self.temp_ttl_minutes < 1:
            problems.append("temp_ttl_minutes must be >= 1")
        if self.allow_cookie_auth and not self.cookie_encryption_key:
            # Not fatal: cookies are then stored with file-mode 0600 only, and the
            # startup doctor warns about it rather than pretending they are encrypted.
            self.extra["cookie_encryption_off"] = True
        if problems:
            raise ConfigError("; ".join(problems))

    @property
    def cookies_encrypted(self) -> bool:
        return bool(self.cookie_encryption_key)

    def to_redacted_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("bot_token", "tg_api_hash", "cookie_encryption_key"):
            if data.get(key):
                data[key] = "***redacted***"
        return data


def _coerce(path: Path) -> Settings:
    """Build Settings from a YAML mapping; unknown keys land in ``extra``."""
    import yaml  # imported lazily so --help works without config files

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")

    known = {f for f in Settings.__dataclass_fields__ if f != "extra"}
    kwargs: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in raw.items():
        if key in known:
            kwargs[key] = Path(value) if key == "data_dir" else value
        else:
            extra[key] = value
    return Settings(**kwargs, extra=extra)


def load_settings(config_file: str | None = None) -> Settings:
    """Load YAML defaults (if present) then apply environment overrides on top."""
    path = Path(config_file or os.environ.get("GRABBER_CONFIG", DEFAULT_CONFIG_FILE))
    settings = _coerce(path) if path.is_file() else Settings()

    overrides: dict[str, Any] = {
        "bot_token": _env("BOT_TOKEN"),
        "send_mode": _env("SEND_MODE") or settings.send_mode,
        "local_api_url": _env("LOCAL_API_URL") or settings.local_api_url,
        "local_files_root": _env("LOCAL_FILES_ROOT") or settings.local_files_root,
        "bot_files_root": _env("BOT_FILES_ROOT") or settings.bot_files_root,
        "tg_api_id": _env_int("TG_API_ID", settings.tg_api_id),
        "tg_api_hash": _env("TG_API_HASH") or settings.tg_api_hash,
        "tg_session_name": _env("TG_SESSION_NAME") or settings.tg_session_name,
        "max_concurrent_downloads": _env_int("MAX_CONCURRENT", settings.max_concurrent_downloads),
        "max_file_mb": _env_int("MAX_FILE_MB", settings.max_file_mb),
        "per_user_concurrent": _env_int("PER_USER_CONCURRENT", settings.per_user_concurrent),
        "temp_ttl_minutes": _env_int("TEMP_TTL_MINUTES", settings.temp_ttl_minutes),
        "dedupe_ttl_minutes": _env_int("DEDUPE_TTL_MINUTES", settings.dedupe_ttl_minutes),
        "use_aria2": _env_bool("USE_ARIA2", settings.use_aria2),
        "aria2c_path": _env("ARIA2C_PATH") or settings.aria2c_path,
        "ffmpeg_path": _env("FFMPEG_PATH") or settings.ffmpeg_path,
        "ffprobe_path": _env("FFPROBE_PATH") or settings.ffprobe_path,
        "allow_cookie_auth": _env_bool("ALLOW_COOKIE_AUTH", settings.allow_cookie_auth),
        "cookie_encryption_key": _env("COOKIE_ENC_KEY") or settings.cookie_encryption_key,
        "max_downloads_per_user_per_hour": _env_int(
            "MAX_DOWNLOADS_PER_HOUR", settings.max_downloads_per_user_per_hour
        ),
        "request_timeout_seconds": _env_int("REQUEST_TIMEOUT", settings.request_timeout_seconds),
        "read_timeout_seconds": _env_int("READ_TIMEOUT", settings.read_timeout_seconds),
    }
    data_dir = _env("DATA_DIR")
    if data_dir:
        overrides["data_dir"] = Path(data_dir)

    allowed = _env("ALLOWED_USER_IDS")
    if allowed:
        overrides["allowed_user_ids"] = tuple(
            int(part) for part in allowed.replace(";", ",").split(",") if part.strip()
        )

    for key, value in overrides.items():
        if value is None or value == "":
            continue
        setattr(settings, key, value)

    settings.validate()
    return settings


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env reader so the bot needs no extra dependency for secrets."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


__all__ = ["Settings", "ConfigError", "load_settings", "load_dotenv"]
