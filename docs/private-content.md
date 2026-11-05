# Private and login-gated content

What this feature is, and precisely what it is not.

## What it does

Some media is only visible to an account: your own feed, a group you belong to, a video you
bought, an age-gated post. If *you* can see it, the engines can fetch it on your behalf —
provided they can prove to the site that the request carries your session.

Two legitimate mechanisms are supported:

1. **Cookies you export from your own logged-in browser session**, uploaded with `/cookies`.
   Passed to yt-dlp as `cookiefile` and to gallery-dl as `--cookies`.
2. **Media already inside Telegram**, i.e. files in conversations the bot can see: forwarded
   or sent messages. Handled by `telegram_media.py` through `getFile`.

## What it does not do, by design

- **No DRM bypass.** If an engine reports `has_drm`, the job raises `DRMMediaError` and the
  registry stops trying other engines. Encrypted streams stay encrypted.
- **No other people's accounts.** A cookie file grants exactly the access its owner has.
  There is no credential-stuffing, no session forgery, no auth-token harvesting from strangers.
- **No official OAuth for the video platforms themselves.** Worth stating plainly, since it
  is often assumed: YouTube, TikTok, Instagram, X and Facebook do not offer a general
  "download this media" OAuth scope to third-party bots. Where a platform *does* have a real
  API, yt-dlp's extractor uses it internally; nothing in this project adds a private channel.
- **No WhatsApp.** WhatsApp's Cloud API only delivers messages your own business account
  sends; there is no official route for a third party to pull media out of a user's chats.
  The routing table records that as an empty engine list with a note, so the bot says so
  instead of failing mysteriously.

## Cookie handling

```
/cookies                       → the bot opens a 3-minute window and asks for the file
upload cookies.txt (document)  → validated, stored per user, window closes
/cookies_clear                 → file and metadata deleted
```

Validation is strict: Netscape `cookies.txt` means seven tab-separated fields in the order
`domain, includeSubdomains, path, secure, expiry, name, value` (verified against
`http.cookiejar.MozillaCookieJar`). A JSON dump or a one-line text file is rejected with the
line number that failed. A wrong file type is refused rather than stored and blamed later.

Storage:

| `COOKIE_ENC_KEY` set | Result |
|---|---|
| yes | file encrypted with Fernet, name `user-<id>.enc`, mode `0600` |
| no | plaintext `user-<id>.txt`, mode `0600`, and `grabber doctor` warns about it |

Turning encryption on and re-uploading deletes the old plaintext copy automatically; a stale
file from the other mode makes `materialize()` raise and ask for a fresh upload instead of
silently using the wrong one. (`os.chmod` is a no-op on Windows — ACLs there are your job.)

Plaintext cookies exist on disk only inside the job directory, for the duration of one
download, and are removed in a `finally` block even when the engine throws.

Cookies are never echoed to the user, never written to logs, and `/cookies` replies with
counts and domains only.

## Expiry and failure signs

Sites rotate sessions. Symptoms that mean "re-upload cookies", not "the bot is broken":

- `E_AUTH` on a link that worked yesterday;
- a much lower quality ladder appearing than the site shows you in the browser;
- YouTube replying *Sign in to confirm you're not a bot* — that is an IP-reputation problem
  on the server, and cookies fix it only some of the time. From a datacenter IP the honest
  answer is usually: run the bot somewhere the site already trusts, or accept the lower tier.

## Access control worth setting

```ini
ALLOWED_USER_IDS=11111,22222       # otherwise anyone can use your bot and your bandwidth
MAX_DOWNLOADS_PER_HOUR=30
ALLOW_COOKIE_AUTH=false            # disable the whole cookie path
```
