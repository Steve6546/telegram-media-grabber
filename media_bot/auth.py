"""Authentication manager: the user's own cookies, stored and handed to the engines.

Scope note written for users, not for the code: cookies unlock content *the uploader
already has access to* (their logged-in feed, their private group, a paid-for video
they bought). They never turn the bot into a way into someone else's account, and DRM
streams stay refused regardless of cookies.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import time
from pathlib import Path

from .db import Database
from .errors import CookieVaultError

log = logging.getLogger("grabber.auth")

NetscapeLine = tuple[str, bool, str, int, str, str]  # domain, includeSub, path, expiry, name, value
_COMMENT = re.compile(r"^\s*#")
DOMAIN_RE = re.compile(r"^[a-z0-9._-]+$", re.I)
MAX_COOKIE_BYTES = 512 * 1024


def parse_netscape(text: str) -> list[NetscapeLine]:
    """Parse a Netscape/http_state cookie file and reject anything malformed."""
    rows: list[NetscapeLine] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or _COMMENT.match(line):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise CookieVaultError(
                f"السطر {number} لا يطابق صيغة Netscape (7 حقول مفصولة بتاب).",
                detail=raw[:120],
            )
        # Verified against http.cookiejar.MozillaCookieJar.save(), which writes:
        # domain, includeSubdomains, path, secure, expiry, name, value
        domain, include_sub, path, _secure, expiry, name, value = (
            parts[0], parts[1].upper() == "TRUE", parts[2], parts[3].upper() == "TRUE",
            parts[4], parts[5], parts[6],
        )
        try:
            expires_at = int(float(expiry)) if expiry not in ("", "0") else 0
        except ValueError as exc:
            raise CookieVaultError(f"انتهاء صلاحية غير رقمي في السطر {number}: {expiry!r}.") from exc
        if not DOMAIN_RE.match(domain):
            raise CookieVaultError(f"نطاق غير صالح في السطر {number}: {domain!r}")
        if not name:
            raise CookieVaultError(f"اسم كوكيز فارغ في السطر {number}.")
        rows.append((domain, include_sub, path or "/", expires_at, name, value))
    if not rows:
        raise CookieVaultError("الملف لا يحتوي أي كوكيز.")
    return rows


def domains_of(rows: list[NetscapeLine]) -> list[str]:
    return sorted({row[0].lstrip(".") for row in rows})


def generate_key() -> str:
    """Produce a valid Fernet key for COOKIE_ENC_KEY."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


class CookieVault:
    def __init__(self, settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.settings.cookies_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = None
        if self.settings.cookie_encryption_key:
            try:
                from cryptography.fernet import Fernet

                self._fernet = Fernet(self.settings.cookie_encryption_key.encode("ascii"))
            except Exception as exc:
                raise CookieVaultError(
                    "COOKIE_ENC_KEY ليس مفتاح Fernet صالحاً؛ أعد توليده بـ grabber keygen.",
                    detail=str(exc),
                ) from exc

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    def _target(self, user_id: int) -> Path:
        suffix = "enc" if self.encrypted else "txt"
        return self.settings.cookies_dir / f"user-{user_id}.{suffix}"

    def _stale_target(self, user_id: int) -> Path:
        """The file the *other* storage mode would have written; must not linger."""
        suffix = "txt" if self.encrypted else "enc"
        return self.settings.cookies_dir / f"user-{user_id}.{suffix}"

    def store(self, user_id: int, content: bytes) -> dict[str, object]:
        if len(content) > MAX_COOKIE_BYTES:
            raise CookieVaultError("ملف الكوكيز كبير بشكل غير معقول.")
        text = content.decode("utf-8", "replace")
        rows = parse_netscape(text)
        payload = text.encode("utf-8")
        if self.encrypted:
            payload = self._fernet.encrypt(payload)  # type: ignore[union-attr]
        target = self._target(user_id)
        target.write_bytes(payload)
        os.chmod(target, 0o600)
        # A plaintext copy from before encryption was enabled would stay readable forever.
        self._stale_target(user_id).unlink(missing_ok=True)
        domains = domains_of(rows)
        self.db.remember_cookies(user_id, domains=domains, entries=len(rows))
        log.info("stored %d cookies for user %s covering %d domains", len(rows), user_id, len(domains))
        return {"entries": len(rows), "domains": domains, "encrypted": self.encrypted,
                "path": target.name}

    def drop(self, user_id: int) -> bool:
        existed = False
        for path in (self._target(user_id), self._stale_target(user_id)):
            if path.exists():
                path.unlink(missing_ok=True)
                existed = True
        self.db.drop_cookies(user_id)
        return existed

    def status(self, user_id: int) -> dict[str, object] | None:
        record = self.db.cookie_status(user_id)
        if not record or not self._target(user_id).exists():
            return None
        record["encrypted"] = self.encrypted
        return record

    def materialize(self, user_id: int, workdir: Path) -> Path | None:
        """Write a plaintext cookie file inside ``workdir`` for the engines, or None.

        Callers must delete the workdir afterwards; cookie plaintext lives there only
        for the duration of one job.
        """
        target = self._target(user_id)
        if not target.exists():
            if self._stale_target(user_id).exists():
                raise CookieVaultError(
                    "ملف الكوكيز محفوظ بالوضع السابق (تغيّر إعداد التشفير). "
                    "أرسل /cookies ثم ارفع الملف من جديد.",
                )
            return None
        raw = target.read_bytes()
        if self.encrypted:
            try:
                from cryptography.fernet import InvalidToken

                raw = self._fernet.decrypt(raw)  # type: ignore[union-attr]
            except InvalidToken as exc:
                raise CookieVaultError(
                    "فشل فك التشفير: هل تغيّر COOKIE_ENC_KEY؟ ارفع الملف من جديد.",
                ) from exc
        plain = workdir / "cookies.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        plain.write_bytes(raw)
        os.chmod(plain, 0o600)
        return plain

    def age_text(self, user_id: int) -> str:
        record = self.db.cookie_status(user_id)
        if not record:
            return "لا يوجد"
        seconds = time.time() - record["stored_at"]
        return f"منذ {int(seconds / 3600)} ساعة" if seconds > 3600 else f"منذ {int(seconds)} ثانية"


__all__ = ["CookieVault", "parse_netscape", "domains_of", "generate_key", "MAX_COOKIE_BYTES"]
