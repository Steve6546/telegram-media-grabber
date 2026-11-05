"""Platform detection and engine routing.

The bot must cover far more than the twelve sites a user happens to name, so this
module holds only a *routing* table (nice labels + which engine to try first) while
the real coverage comes from whatever the installed engines support. Unknown hosts
fall through to the generic path instead of being rejected, which is what makes
hundreds of extra sites work with no code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .extractors.base import MediaKind, Want


@dataclass(slots=True)
class Platform:
    key: str
    label: str
    hosts: tuple[str, ...]
    #: engine ids in preference order; unknown entries are skipped by the registry
    engines: tuple[str, ...] = ("ytdlp",)
    kinds: tuple[MediaKind, ...] = (MediaKind.VIDEO, MediaKind.AUDIO)
    needs_login_common: bool = False
    notes: str = ""

    def matches(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for pattern in self.hosts:
            if host == pattern or host.endswith("." + pattern):
                return True
        return False


PLATFORMS: tuple[Platform, ...] = (
    Platform("youtube", "YouTube", ("youtube.com", "youtu.be", "music.youtube.com"),
             kinds=(MediaKind.VIDEO, MediaKind.AUDIO),
             notes="قد يطلب تأكيد هوية من المتصفح على بعض الفيديوهات"),
    Platform("tiktok", "TikTok", ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
             engines=("ytdlp", "gallery-dl"), needs_login_common=True),
    Platform("instagram", "Instagram", ("instagram.com", "instagr.am"),
             engines=("ytdlp", "gallery-dl"),
             kinds=(MediaKind.VIDEO, MediaKind.ALBUM, MediaKind.IMAGE),
             needs_login_common=True,
             notes="الصور والألبومات غالباً تحتاج cookies"),
    Platform("facebook", "Facebook", ("facebook.com", "fb.watch", "fb.com", "meta.com"),
             engines=("ytdlp", "gallery-dl"), needs_login_common=True),
    Platform("x", "X / Twitter", ("twitter.com", "x.com", "t.co"),
             engines=("ytdlp", "gallery-dl"),
             kinds=(MediaKind.VIDEO, MediaKind.IMAGE, MediaKind.ALBUM),
             needs_login_common=True),
    Platform("reddit", "Reddit", ("reddit.com", "redd.it", "v.redd.it"),
             engines=("ytdlp", "gallery-dl"), kinds=(MediaKind.VIDEO, MediaKind.IMAGE, MediaKind.ALBUM)),
    Platform("pinterest", "Pinterest", ("pinterest.com", "pin.it", "pinterest.co.uk",
                                        "pinterest.ca", "pinterest.de", "pinterest.fr"),
             engines=("gallery-dl", "ytdlp"),
             kinds=(MediaKind.IMAGE, MediaKind.ALBUM, MediaKind.VIDEO),
             needs_login_common=True),
    Platform("snapchat", "Snapchat", ("snapchat.com", "storysnap.com"),
             engines=("ytdlp",), kinds=(MediaKind.VIDEO),
             notes="المُستخرج يغطي Snapchat Spotlight فقط، ولا يغطي اللقطات الخاصة"),
    Platform("twitch", "Twitch", ("twitch.tv", "clips.twitch.tv", "m.twitch.tv"),
             engines=("ytdlp",), kinds=(MediaKind.VIDEO, MediaKind.LIVE)),
    Platform("vimeo", "Vimeo", ("vimeo.com", "player.vimeo.com"), engines=("ytdlp",)),
    Platform("dailymotion", "Dailymotion", ("dailymotion.com", "dai.ly"), engines=("ytdlp",)),
    Platform("soundcloud", "SoundCloud", ("soundcloud.com", "on.soundcloud.com"),
             engines=("ytdlp",), kinds=(MediaKind.AUDIO)),
    Platform("imgur", "Imgur", ("imgur.com", "i.imgur.com"),
             engines=("gallery-dl", "ytdlp"),
             kinds=(MediaKind.IMAGE, MediaKind.ALBUM, MediaKind.VIDEO)),
    Platform("tumblr", "Tumblr", ("tumblr.com",), engines=("gallery-dl",),
             kinds=(MediaKind.IMAGE, MediaKind.ALBUM, MediaKind.VIDEO)),
    Platform("flickr", "Flickr", ("flickr.com", "flickr.net"), engines=("gallery-dl",),
             kinds=(MediaKind.IMAGE, MediaKind.ALBUM)),
    Platform("deviantart", "DeviantArt", ("deviantart.com", "fav.me", "sta.sh"),
             engines=("gallery-dl",), kinds=(MediaKind.IMAGE, MediaKind.ALBUM)),
    Platform("unsplash", "Unsplash", ("unsplash.com",), engines=("gallery-dl",),
             kinds=(MediaKind.IMAGE,)),
    Platform("artstation", "ArtStation", ("artstation.com", "sta.sh"), engines=("gallery-dl",),
             kinds=(MediaKind.IMAGE, MediaKind.ALBUM)),
    Platform("weibo", "Weibo", ("weibo.com",), engines=("gallery-dl",),
             kinds=(MediaKind.IMAGE, MediaKind.ALBUM), needs_login_common=True),
    Platform("koo", "Koo", ("kooapp.com",), engines=("ytdlp",)),
    Platform("linkedin", "LinkedIn", ("linkedin.com", "lnkd.in"), engines=("ytdlp",),
             needs_login_common=True),
    Platform("ok", "OK.ru", ("ok.ru",), engines=("ytdlp",)),
    Platform("bilibili", "Bilibili", ("bilibili.com", "b23.tv"), engines=("ytdlp",)),
    Platform("streamable", "Streamable", ("streamable.com",), engines=("ytdlp",)),
    Platform("rumble", "Rumble", ("rumble.com",), engines=("ytdlp",)),
    Platform("wistia", "Wistia", ("wistia.com", "fast.wistia.com"), engines=("ytdlp",)),
    Platform("loom", "Loom", ("loom.com",), engines=("ytdlp",)),
    Platform("yad2", "Yad2", ("yad2.co.il",), engines=("ytdlp",)),
    Platform("arirang", "KBS/ARTE/CBC broadcasters", ("arte.tv", "cbc.ca", "ctv.ca"),
             engines=("ytdlp",), kinds=(MediaKind.VIDEO, MediaKind.LIVE)),
    Platform("bandcamp", "Bandcamp", ("bandcamp.com",), engines=("ytdlp", "gallery-dl"),
             kinds=(MediaKind.AUDIO, MediaKind.IMAGE)),
    Platform("telegram_embed", "Telegram public post", ("t.me", "telegram.me"),
             engines=("ytdlp",), kinds=(MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.IMAGE),
             notes="المنشورات العامة فقط؛ الوسائط داخل محادثاتك تُرسل للمbot مباشرة"),
    Platform("whatsapp", "WhatsApp", ("whatsapp.com", "wa.me"), engines=(),
             kinds=(MediaKind.VIDEO,),
             notes="لا يوجد مُستخرج عام: WhatsApp لا يوفّر واجهة رسمية لتحميل وسائط المحادثات"),
)

_BY_KEY = {platform.key: platform for platform in PLATFORMS}
GENERIC = Platform("generic", "موقع عام", (), engines=("ytdlp", "gallery-dl", "direct"))
DIRECT = Platform("direct", "ملف مباشر", (), engines=("direct", "ytdlp"))

_IMAGE_HOST_HINT = re.compile(r"(^|\.)(cdn|images?|media|photos?|pics|thumb)", re.I)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def detect(url: str, *, want: Want = Want.AUTO) -> Platform:
    host = host_of(url)
    if not host:
        return GENERIC
    if "." not in host:
        return GENERIC
    for platform in PLATFORMS:
        if platform.matches(host):
            return platform
    return GENERIC


def engine_order(platform: Platform, *, want: Want = Want.AUTO) -> tuple[str, ...]:
    """Which engines to try, in order, for this platform and media appetite."""
    engines = list(platform.engines) or ["ytdlp", "gallery-dl", "direct"]
    if want is Want.IMAGE and "gallery-dl" in engines:
        engines.sort(key=lambda name: 0 if name == "gallery-dl" else 1)
    if want is Want.IMAGE and "gallery-dl" not in engines:
        engines.insert(0, "gallery-dl")
    if want in (Want.VIDEO, Want.AUDIO) and "ytdlp" in engines:
        engines.sort(key=lambda name: 0 if name == "ytdlp" else 1)
    if want is Want.AUTO and "direct" not in engines:
        engines.append("direct")
    return tuple(dict.fromkeys(engines))


def coverage_report() -> dict[str, object]:
    """Live counts from the installed engines — the honest answer to "how many sites"."""
    report: dict[str, object] = {"routing_table": len(PLATFORMS)}
    try:
        from yt_dlp.extractor import gen_extractor_classes

        classes = [cls for cls in gen_extractor_classes() if not cls.__name__.startswith("_")]
        report["ytdlp_extractors"] = len(classes)
    except Exception as exc:  # pragma: no cover - engine missing
        report["ytdlp_error"] = str(exc)
    try:
        import subprocess
        import sys

        out = subprocess.run([sys.executable, "-m", "gallery_dl", "--list-extractors"],
                             capture_output=True, text=True, timeout=120).stdout
        report["gallery_dl_extractors"] = len(re.findall(r"^Extractor for", out, re.M))
    except Exception as exc:  # pragma: no cover - engine missing
        report["gallery_dl_error"] = str(exc)
    return report


def platform_label(url: str) -> str:
    return detect(url).label


__all__ = ["Platform", "PLATFORMS", "GENERIC", "DIRECT", "detect", "host_of",
           "engine_order", "coverage_report", "platform_label"]
