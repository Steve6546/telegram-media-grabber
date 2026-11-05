"""Cookie vault: format validation, encryption, and the plaintext window."""

from __future__ import annotations

import os
import stat

import pytest

from media_bot.auth import CookieVault, domains_of, generate_key, parse_netscape
from media_bot.errors import CookieVaultError

VALID = """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tFALSE\t1900000000\tsessionid\tabc123
# comment line
.tiktok.com\tTRUE\t/\tTRUE\t0\ttt_webid\t999
.example.com\tFALSE\t/path\tTRUE\t0\tcsrf\txyz
"""


def test_parse_valid_file() -> None:
    rows = parse_netscape(VALID)
    assert len(rows) == 3
    assert rows[0][0] == ".instagram.com"
    assert rows[0][1] is True
    assert rows[0][4] == "sessionid" and rows[0][5] == "abc123"
    # an expiry of 0 is a session cookie, not a number-parsing error
    assert rows[1][3] == 0


def test_domains_are_normalised() -> None:
    assert domains_of(parse_netscape(VALID)) == ["example.com", "instagram.com", "tiktok.com"]


@pytest.mark.parametrize("bad", [
    "not a cookie file at all",
    "",
    "# Netscape HTTP Cookie File\n",
    "a\tb\tc\td\te\tf",              # 6 fields, not 7
    ".ok.com\tTRUE\t/\tnotanumber\tk\tv",
    "bad domain.com\tTRUE\t/\t0\tk\tv",
    ".ok.com\tTRUE\t/\t0\t\tvalue",   # empty name
])
def test_malformed_input_is_rejected(bad: str) -> None:
    with pytest.raises(CookieVaultError):
        parse_netscape(bad)


def test_plaintext_store_and_materialize(settings, db, vault: CookieVault, tmp_path) -> None:
    assert vault.encrypted is False
    info = vault.store(7, VALID.encode())
    assert info["entries"] == 3 and info["encrypted"] is False
    path = vault._target(7)
    assert path.suffix == ".txt"
    if os.name == "posix":
        # Windows chmod only toggles the read-only bit, so a 600 assertion there is theatre.
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    plain = vault.materialize(7, tmp_path / "work")
    assert plain and "sessionid" in plain.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(plain).st_mode) == 0o600


def test_encrypted_roundtrip(settings, db) -> None:
    settings.cookie_encryption_key = generate_key()
    vault = CookieVault(settings, db)
    assert vault.encrypted is True
    info = vault.store(9, VALID.encode())
    assert info["encrypted"] is True
    raw = vault._target(9).read_bytes()
    assert b"sessionid" not in raw, "cookies must not be readable on disk when a key is set"
    plain = vault.materialize(9, settings.cache_dir / "w")
    assert b"sessionid" in plain.read_bytes()


def test_wrong_key_is_reported_not_silently_ignored(settings, db) -> None:
    settings.cookie_encryption_key = generate_key()
    vault = CookieVault(settings, db)
    vault.store(11, VALID.encode())
    settings.cookie_encryption_key = generate_key()
    rotated = CookieVault(settings, db)
    with pytest.raises(CookieVaultError):
        rotated.materialize(11, settings.cache_dir / "w2")


def test_invalid_key_fails_at_construction(settings, db) -> None:
    settings.cookie_encryption_key = "clearly-not-a-fernet-key"
    with pytest.raises(CookieVaultError):
        CookieVault(settings, db)


def test_plaintext_file_with_key_configured_is_refused(settings, db) -> None:
    from dataclasses import replace

    vault_plain = CookieVault(replace(settings, cookie_encryption_key=""), db)
    vault_plain.store(13, VALID.encode())
    settings.cookie_encryption_key = generate_key()
    vault_enc = CookieVault(settings, db)
    with pytest.raises(CookieVaultError):
        vault_enc.materialize(13, settings.cache_dir / "w3")


def test_status_and_drop(settings, db, vault: CookieVault) -> None:
    assert vault.status(3) is None
    vault.store(3, VALID.encode())
    status = vault.status(3)
    assert status["entries"] == 3 and "instagram.com" in status["domains"]
    assert vault.drop(3) is True
    assert vault.status(3) is None
    assert vault.drop(3) is False


def test_materialize_without_cookies_returns_none(settings, db, vault: CookieVault, tmp_path) -> None:
    assert vault.materialize(99, tmp_path / "nothing") is None


def test_oversized_cookie_file_is_refused(settings, db, vault: CookieVault) -> None:
    from media_bot.auth import MAX_COOKIE_BYTES

    with pytest.raises(CookieVaultError):
        vault.store(1, b"# Netscape HTTP Cookie File\n" + b"x" * (MAX_COOKIE_BYTES + 1))


def test_generated_keys_are_unique_and_usable() -> None:
    from cryptography.fernet import Fernet

    keys = {generate_key() for _ in range(5)}
    assert len(keys) == 5
    for key in keys:
        Fernet(key.encode())

def test_stale_plaintext_is_removed_when_encryption_is_enabled(settings, db) -> None:
    from dataclasses import replace

    plain = CookieVault(replace(settings, cookie_encryption_key=""), db)
    plain.store(21, VALID.encode())
    assert plain._target(21).exists()

    encrypted = CookieVault(replace(settings, cookie_encryption_key=generate_key()), db)
    encrypted.store(21, VALID.encode())
    # the pre-encryption plaintext copy must not survive on disk
    assert not (settings.cookies_dir / "user-21.txt").exists()
    assert (settings.cookies_dir / "user-21.enc").exists()
    assert encrypted.materialize(21, settings.cache_dir / "w").read_bytes() == VALID.encode()


def test_parser_accepts_a_file_written_by_the_stdlib(settings, db, tmp_path) -> None:
    """Ground truth: a cookies.txt produced by http.cookiejar must parse."""
    import http.cookiejar as cookiejar

    path = tmp_path / "real.txt"
    jar = cookiejar.MozillaCookieJar(str(path))
    jar.set_cookie(cookiejar.Cookie(
        version=0, name="sessionid", value="abc123", port=None, port_specified=False,
        domain=".example.com", domain_specified=True, domain_initial_dot=True,
        path="/path", path_specified=True, secure=False, expires=2000000000,
        discard=False, comment=None, comment_url=None, rest={}, rfc2109=False))
    jar.save(ignore_discard=True, ignore_expires=True)

    rows = parse_netscape(path.read_text(encoding="utf-8"))
    assert rows[0][0] == ".example.com"
    assert rows[0][3] == 2000000000, "expiry must be read from the 5th field, not the 4th"
    assert rows[0][4] == "sessionid" and rows[0][5] == "abc123"


def test_drop_removes_both_storage_modes(settings, db) -> None:
    from dataclasses import replace

    plain = CookieVault(replace(settings, cookie_encryption_key=""), db)
    plain.store(22, VALID.encode())
    encrypted = CookieVault(replace(settings, cookie_encryption_key=generate_key()), db)
    encrypted.store(22, VALID.encode())
    assert encrypted.drop(22) is True
    assert not (settings.cookies_dir / "user-22.enc").exists()
    assert encrypted.status(22) is None

