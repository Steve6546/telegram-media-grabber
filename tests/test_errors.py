"""Error classification: raw engine text must become an actionable, honest error."""

from __future__ import annotations

import pytest

from media_bot import errors


@pytest.mark.parametrize("raw,expected", [
    ("ERROR: no video formats available, please provide cookies", errors.AuthRequiredError),
    ("ERROR: this video is private", errors.AuthRequiredError),
    ("ERROR: Sign in to confirm you're not a bot", errors.AuthRequiredError),
    ("HTTP Error 403: Forbidden", errors.AuthRequiredError),
    ("content is protected by widevine DRM", errors.DRMMediaError),
    ("HTTP Error 429: Too Many Requests", errors.RateLimitedError),
    ("HTTP Error 404: Not Found", errors.NoMediaFoundError),
    ("ERROR: Unsupported URL: https://example.com", errors.UnsupportedPlatformError),
    ("The read operation timed out", errors.TimeoutError_),
    ("[Errno -2] Name or service not known", errors.NetworkError),
    ("Requested format is not available", errors.NoMediaFoundError),
    ("something entirely unexpected", errors.EngineError),
])
def test_classify_maps_text_to_typed_errors(raw: str, expected: type) -> None:
    exc = errors.classify(raw)
    assert isinstance(exc, expected)
    assert exc.detail == raw


def test_classify_preserves_detail_for_debugging() -> None:
    exc = errors.classify("HTTP Error 403: Forbidden")
    assert "403" in exc.detail
    assert exc.code == "E_AUTH"


@pytest.mark.parametrize("exc,code", [
    (errors.BlockedHostError(), "E_SSRF"),
    (errors.DRMMediaError(), "E_DRM"),
    (errors.FileTooLargeError(), "E_SIZE"),
    (errors.QueueFullError(), "E_QUEUE"),
])
def test_describe_returns_code_message_action(exc, code) -> None:
    got_code, message, action = errors.describe(exc)
    assert got_code == code
    assert message and action
    assert "Traceback" not in message


def test_describe_handles_unknown_exceptions_without_leaking_internals() -> None:
    code, message, action = errors.describe(ValueError("secret internal path /etc/passwd"))
    assert code == "E_UNK"
    assert "ValueError" in message
    assert "/etc/passwd" not in message


def test_every_error_is_grabber_error() -> None:
    for name in errors.__all__:
        obj = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, BaseException) and obj is not errors.GrabberError:
            assert issubclass(obj, errors.GrabberError)


def test_login_patterns_do_not_match_benign_words() -> None:
    """The old substring list matched 'message' and 'storage' via 'age'."""
    for benign in ("ERROR: unable to obtain certificate chain",
                   "ERROR: unsupported url for this storage backend",
                   "message too large"):
        exc = errors.classify(benign)
        assert not isinstance(exc, errors.AuthRequiredError), benign
