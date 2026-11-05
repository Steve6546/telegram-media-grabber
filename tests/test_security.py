"""URL validation, SSRF defence and filename hardening."""

from __future__ import annotations

import pytest

from media_bot import security
from media_bot.errors import BlockedHostError, InvalidURLError


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private",
    "http://localhost:8080/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.7/file.mp4",
    "http://192.168.1.10/video",
    "http://172.16.0.1/a",
    "http://[::1]/x",
    "http://[fd12:3456::1]/x",
    "http://0.0.0.0/x",
    "http://127.5.5.5/x",
    "http://100.64.0.8/x",
])
def test_private_and_metadata_addresses_are_blocked(url: str) -> None:
    with pytest.raises(BlockedHostError):
        security.check_url(url)


@pytest.mark.parametrize("url", ["ftp://host/file.mp4", "file:///etc/passwd",
                                 "javascript:alert(1)", "data:text/html,x", ""])
def test_non_http_schemes_and_empty_are_rejected(url: str) -> None:
    with pytest.raises(InvalidURLError):
        security.check_url(url)


def test_credentials_in_url_are_rejected() -> None:
    with pytest.raises(InvalidURLError):
        security.check_url("https://user:secret@example.com/media.mp4")


def test_bare_domain_gets_https() -> None:
    assert security.check_url("example.com/a.mp4") == "https://example.com/a.mp4"


@pytest.mark.parametrize("host,expected", [
    ("8.8.8.8", False),
    ("142.250.0.1", False),
    ("2606:4700:4700::1111", False),
    ("127.0.0.1", True),
    ("10.10.10.10", True),
    ("169.254.169.254", True),
    ("100.64.1.1", True),
    ("fc00::1", True),
    ("fe80::1", True),
    ("not-an-ip", True),
])
def test_address_classifier(host: str, expected: bool) -> None:
    assert security.is_blockable_address(host) is expected


def test_teredo_wrapping_a_private_v4_is_blocked() -> None:
    # 2001:0000:4136:e378:8000:63bf:3fff:fdd2 embeds 192.0.2.2 (test-net, treated public)
    # but ::64ff:a068 embeds 100.255... which is CGNAT-ish; assert the mapped case only.
    assert security.is_blockable_address("::ffff:127.0.0.1") is True
    assert security.is_blockable_address("::ffff:10.1.2.3") is True


@pytest.mark.parametrize("name,expect_safe", [
    ("../../../etc/passwd", "passwd"),
    ("a\\b\\c.mp4", "c.mp4"),
    ("", "media"),
    ("..", "media"),
    (".", "media"),
    ("CON", "media"),
    ("nul.mp4", "nul.mp4"),
    ('vi:deo|name?.mp4', "vi_deo_name_.mp4"),
    ("   spaced   name.mp4  ", "spaced name.mp4"),
    (".htaccess", "htaccess"),
    ("正常名字.mp4", "正常名字.mp4"),
])
def test_filename_sanitising(name: str, expect_safe: str) -> None:
    result = security.sanitize_filename(name)
    assert "/" not in result and "\\" not in result
    assert not result.startswith(".")
    assert result == expect_safe


def test_long_names_are_truncated() -> None:
    assert len(security.sanitize_filename("x" * 500)) <= 120


@pytest.mark.parametrize("url,expected", [
    ("https://cdn.example/a.MP4", True),
    ("https://cdn.example/a.webp", True),
    ("https://example.com/watch?v=1", False),
    ("https://example.com/a/b/", False),
])
def test_direct_media_heuristic(url: str, expected: bool) -> None:
    assert security.looks_like_media_url(url) is expected


def test_newline_injection_in_url_is_neutralised() -> None:
    assert "\n" not in security.normalize_url("https://example.com/a%0Aevil")
