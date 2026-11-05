"""Input hardening: URL validation, SSRF defence and safe filenames.

Everything the bot fetches is fetched because a user pasted a URL, so this module is
the trust boundary. A URL is accepted only when its scheme is http(s), it carries a
host, it resolves (through every A/AAAA record) to a public address, and it contains
no embedded credentials.

Known residual risk, documented rather than hidden: resolving here and resolving
inside yt-dlp/gallery-dl are two separate lookups, so a hostile DNS record with a
0-second TTL could still move to a private address between the two (classic
time-of-check/time-of-use). Defeating that needs a custom resolver/proxy in front of
the engines, which this project does not ship.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, unquote

from .errors import BlockedHostError, InvalidURLError

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are never fetched, even if they would resolve publicly.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "wpad",
    }
)

def is_blockable_address(ip: str) -> bool:
    """True when ``ip`` must not be fetched: loopback, RFC1918, ULA, link-local,
    carrier-grade NAT, multicast, reserved, or the cloud metadata ranges."""
    try:
        addr = ipaddress.ip_address(ip.strip("[]"))
    except ValueError:
        return True  # unparsable -> refuse
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    # 100.64.0.0/10 (carrier-grade NAT) is not flagged as private by the stdlib.
    if addr.version == 4 and addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return True
    # IPv6 unique-local fc00::/7 and 6to4 Teredo carrying v4 private addresses.
    if addr.version == 6:
        if addr in ipaddress.IPv6Network("fc00::/7") or addr in ipaddress.IPv6Network("::/96"):
            return True
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            return is_blockable_address(str(mapped))
        teredo = getattr(addr, "teredo", None)
        if teredo is not None and teredo[1] is not None:
            return is_blockable_address(str(teredo[1]))
    return False


def normalize_url(raw: str) -> str:
    cleaned = (raw or "").strip().strip("<>")
    for invisible in ("​", "‌", "‍", "﻿"):
        cleaned = cleaned.replace(invisible, "")
    # Deliberately no unquote(): decoding the whole URL can turn %0A/%0D or %20 into real
    # control characters and spaces, which then split the URL or smuggle a second one.
    # Percent-encoding is left for the HTTP layer, which handles it correctly per component.
    if not cleaned:
        raise InvalidURLError("الرابط فارغ.")
    if re.match(r"^[a-z][a-z0-9+.-]*://", cleaned, re.I) is None:
        # Bare domain like youtu.be/xyz is what users actually paste.
        if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/|$)", cleaned, re.I):
            cleaned = "https://" + cleaned
        else:
            raise InvalidURLError("لا يشبه رابطاً: يجب أن يبدأ بـ http/https أو بعنوان موقع.")
    if any(ord(char) < 0x20 or char.isspace() for char in cleaned):
        raise InvalidURLError("الرابط يحتوي محارف تحكم أو فراغات.")
    return cleaned


def check_url(raw: str) -> str:
    """Validate ``raw`` and return the normalized URL.

    Raises InvalidURLError / BlockedHostError. Always use through ``asyncio.to_thread``
    in the request path, since DNS lookups are blocking. There is deliberately no
    "skip resolution" switch: a caller that disables it disables the SSRF defence.
    """
    url = normalize_url(raw)
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        shown = parts.scheme or "(none)"
        raise InvalidURLError(f"المخطط {shown} غير مسموح.")
    if not parts.hostname:
        raise InvalidURLError("الرابط بلا اسم نطاق.")
    if parts.username or parts.password:
        # Credentials in a URL are both a leak vector and a phishing pattern.
        raise InvalidURLError("روابط تحتوي بيانات دخول مضمّنة غير مقبولة.")
    host = parts.hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTNAMES or host.endswith((".localhost", ".internal", ".local")):
        raise BlockedHostError(f"اسم المضيف {host} محظور صراحة.")

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedHostError(f"تعذر حل اسم المضيف {host}.", detail=str(exc)) from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise BlockedHostError(f"لا عناوين للنطاق {host}.")
    for address in addresses:
        if is_blockable_address(address):
            raise BlockedHostError(f"النطاق {host} يحل إلى عنوان خاص ({address}).")
    return url


def sanitize_filename(name: str, *, fallback: str = "media", limit: int = 120) -> str:
    """Strip traversal and OS-dangerous characters; never returns an absolute path."""
    text = unquote(name or "").replace("\\", "/").split("/")[-1]
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r'[<>:"|?*]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if text.lower() in {"", ".", "..", "con", "prn", "aux", "nul"} or re.match(r"^com\d$|^lpt\d$", text.lower()):
        text = ""
    text = text[:limit].rstrip()
    return text or fallback


def looks_like_media_url(url: str) -> bool:
    """Cheap heuristic used to decide whether a direct-file fetch is worth trying."""
    path = urlsplit(url).path.lower()
    return bool(re.search(r"\.(mp4|m4v|mkv|webm|mov|avi|mp3|m4a|opus|ogg|wav|flac|aac|"
                          r"jpg|jpeg|png|gif|webp|heic|avif|bmp|tiff|pdf|zip|epub)$", path))


__all__ = ["ALLOWED_SCHEMES", "check_url", "normalize_url", "sanitize_filename",
           "is_blockable_address", "looks_like_media_url"]
