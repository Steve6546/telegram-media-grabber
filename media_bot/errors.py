"""Typed failures, each carrying a message the user can act on.

Rule of this module: an internal exception is never surfaced raw, and no error is
reported as success. ``describe()`` turns any exception into (text, suggested_action).
"""

from __future__ import annotations


class GrabberError(Exception):
    """Base for every expected failure."""

    #: short code shown to the user so reports are searchable
    code = "error"
    user_message = "حدث خطأ غير متوقع."
    action = "أعد المحاولة، وإن تكرر الرمز راجع السجل."

    def __init__(self, message: str | None = None, *, detail: str = "") -> None:
        super().__init__(message or self.user_message)
        self.detail = detail

    def as_pair(self) -> tuple[str, str]:
        return self.user_message, self.action


class InvalidURLError(GrabberError):
    code = "E_URL"
    user_message = "الرابط غير صالح."
    action = "أرسل رابطاً كاملاً يبدأ بـ http:// أو https://"


class BlockedHostError(GrabberError):
    """SSRF defence: the URL resolves to a private/loopback/link-local address."""

    code = "E_SSRF"
    user_message = "الرابط يشير إلى عنوان شبكي خاص، والمُنع هنا مقصود."
    action = "أرسل رابطاً عاماً على الإنترنت."


class UnsupportedPlatformError(GrabberError):
    code = "E_PLATFORM"
    user_message = "هذه المنصة غير مدعومة من محركات التنزيل الحالية."
    action = "جرّب رابطاً مباشراً لملف وسائط (.mp4/.jpg/...) أو منصة معروفة."


class NoMediaFoundError(GrabberError):
    code = "E_NOMEDIA"
    user_message = "لم يُعثر على وسائط قابلة للتنزيل خلف هذا الرابط."
    action = "تأكد أن المحتوى عام وأن الرابط لا يتطلب صفحة دخول."


class AuthRequiredError(GrabberError):
    code = "E_AUTH"
    user_message = "هذا المحتوى خاص ويحتاج إلى إثبات هوية يملكه أنت."
    action = "استخدم /cookies لرفع ملف cookies خاصتك، أو أرسل محتوى عاماً."


class RateLimitedError(GrabberError):
    code = "E_LIMIT"
    user_message = "المنصة ترفض طلبات أكثر في هذه اللحظة (429)."
    action = "انتظر دقيقة ثم أعد المحاولة."


class DRMMediaError(GrabberError):
    """Encrypted media is out of scope by design and is reported, not bypassed."""

    code = "E_DRM"
    user_message = "المحتوى محمي بتشفير DRM، والبوت لا يتجاوز الحماية."
    action = "استخدم النسخة العامة غير المحمية إن وُجدت."


class FileTooLargeError(GrabberError):
    code = "E_SIZE"
    user_message = "الملف أكبر من الحد المسموح إرساله عبر تيليجرام بهذه الطريقة."
    action = "انزل جودة أقل، أو صوت فقط، أو فعّل Local Bot API Server / MTProto."


class CancelledJobError(GrabberError):
    code = "E_CANCEL"
    user_message = "أُلغيت العملية."
    action = "أرسل الرابط من جديد إن أردت المتابعة."


class QueueFullError(GrabberError):
    code = "E_QUEUE"
    user_message = "طابور التنزيل ممتلئ الآن."
    action = "انتظر إنتهاء مهمة ثم أعد المحاولة."


class TimeoutError_(GrabberError):
    code = "E_TIMEOUT"
    user_message = "انتهت مهلة الاتصال بالمنصة."
    action = "جرّب رابطاً آخر أو أعد المحاولة لاحقاً."


class NetworkError(GrabberError):
    code = "E_NET"
    user_message = "تعذر الوصول إلى المنصة من هذا الخادم."
    action = "قد يكون السبب حجب الشبكة أو جدار حماية أو عنوان IP مرفوض."


class EngineError(GrabberError):
    code = "E_ENGINE"
    user_message = "فشل محرك التنزيل أثناء استخراج هذا الرابط."
    action = "جرّب المحرك الآخر أو أعد المحاولة بجودة أقل."


class SenderError(GrabberError):
    code = "E_SEND"
    user_message = "نجح التنزيل لكن تيليجرام رفض الاستلام."
    action = "فعّل مسار الملفات الكبيرة (Local API أو MTProto) ثم أعد المحاولة."


class CookieVaultError(GrabberError):
    code = "E_COOKIE"
    user_message = "تعذرت قراءة أو فك تشفير ملف الـ cookies."
    action = "أرسل /cookies مرة أخرى بملف Netscape صالح، أو /cookies_clear."


class StorageError(GrabberError):
    code = "E_STORAGE"
    user_message = "تعذرت الكتابة على القرص."
    action = "تحقق من مساحة disk ومن صلاحيات مجلد data."


# ---------------------------------------------------------------------------
# Mapping raw engine output onto the typed errors above.

_PATTERNS: tuple[tuple[tuple[str, ...], type[GrabberError]], ...] = (
    (("fresh cookies", "provide cookies", "--cookies", "cookies to provide", "login required",
      "log in", "logged-in", "logged in", "sign in", "video is private", "private video",
      "members-only", "account needs to be logged in", "http error 401",
      "http error 403: forbidden", "authentication required"),
     AuthRequiredError),
    (("drm", "widevine", "protected by"), DRMMediaError),
    (("http error 429", "too many requests", "rate limit"), RateLimitedError),
    (("http error 404", "not found", "does not exist"), NoMediaFoundError),
    (("unsupported url", "no working extractor", "unrecognized", "extractor not found"),
     UnsupportedPlatformError),
    (("timed out", "timeout", "read timeout"), TimeoutError_),
    (("getaddrinfo failed", "name or service not known", "no route to host",
      "connection refused", "network is unreachable", "ssl", "certificate"), NetworkError),
    (("requested format is not available", "no formats", "is not a valid format"),
     NoMediaFoundError),
)


def classify(raw: str | None, *, fallback: type[GrabberError] = EngineError) -> GrabberError:
    """Turn a downloader traceback string into a typed error, keeping the raw detail."""
    text = (raw or "").strip()
    lowered = text.lower()
    for needles, kind in _PATTERNS:
        if any(needle in lowered for needle in needles):
            return kind(detail=text)
    return fallback(detail=text)


def describe(exc: BaseException) -> tuple[str, str, str]:
    """Return (code, message, action) for any exception, including unknown ones."""
    if isinstance(exc, GrabberError):
        message, action = exc.as_pair()
        return exc.code, message, action
    if isinstance(exc, TimeoutError):
        return TimeoutError_.code, TimeoutError_.user_message, TimeoutError_.action
    if isinstance(exc, PermissionError):
        return StorageError.code, StorageError.user_message, StorageError.action
    name = type(exc).__name__
    return "E_UNK", f"خطأ داخلي غير مصنف ({name}).", "راجع السجل مطولاً وأرسل اسم الخطأ."


__all__ = [
    "GrabberError", "InvalidURLError", "BlockedHostError", "UnsupportedPlatformError",
    "NoMediaFoundError", "AuthRequiredError", "RateLimitedError", "DRMMediaError",
    "FileTooLargeError", "CancelledJobError", "QueueFullError", "TimeoutError_",
    "NetworkError", "EngineError", "SenderError", "CookieVaultError", "StorageError",
    "classify", "describe",
]
