# Extending: platforms, engines, media kinds

The design goal is that supporting another site is a local change. Three levels, cheapest
first.

## Level 0 — nothing to do

`yt-dlp` ships 1863 extractors and `gallery-dl` 233 categories (measured on the installed
versions: `grabber doctor` prints the live counts). A URL for a site neither knows still
reaches yt-dlp's `generic` extractor, then gallery-dl, then the direct-file engine. Most
"unsupported site" reports are actually auth or IP-reputation problems, which the error
codes distinguish.

## Level 1 — nicer label, or different engine preference

Edit `media_bot/platform_detector.py` and add one entry:

```python
Platform(
    "kick", "Kick", ("kick.com", "kiwibox.com"),
    engines=("ytdlp",),                    # preference order
    kinds=(MediaKind.VIDEO, MediaKind.LIVE),
    notes="البث المسجل يظهر بعد توفره على الصفحة",
)
```

That changes routing and display only. No handler, queue, storage or sender code is touched,
and the existing tests for routing keep covering it.

`engine_order()` respects `Want`: an explicit "audio" or "image" request reorders candidates,
so `image` appetite promotes gallery-dl even on a site where yt-dlp normally runs first.

## Level 2 — a new engine or a new media kind

Subclass `media_bot.extractors.base.Extractor` and register it in
`media_bot/extractors/registry.py:build_registry()`:

```python
class MyEngine(Extractor):
    engine_id = "mysite"
    provides = frozenset({MediaKind.ALBUM})
    priority = 15

    def supports(self, host, url, *, want=Want.AUTO) -> bool: ...
    async def extract(self, url, *, host="", cookies_path=None, want=Want.AUTO) -> Bundle: ...
    async def download(self, bundle, variant, dest_dir, *, progress=None,
                       cookies_path=None) -> DownloadResult: ...
```

Rules the contract imposes, because they are what makes the rest of the bot honest:

- **Only report what the site returned.** `Bundle.variants` comes from real format entries —
  go through `quality.build_variants()` rather than hand-writing rows, and you inherit the
  guarantees (no invented heights, real or estimated sizes, `merge` set when streams are
  separate).
- **One action per button.** Tokens must fit Telegram's 64-byte `callback_data`:
  `q:<12-char bundle token>:<variant token>`. There is a test that raises if a combination
  cannot fit.
- **Fail loudly with a typed error.** Raise from `media_bot.errors`
  (`AuthRequiredError`, `DRMMediaError`, `RateLimitedError`, `UnsupportedPlatformError`, …)
  so `describe()` can give the user a code and a next step. Raising a raw exception becomes
  `E_UNK`, which is the outcome to avoid.
- **Never bypass DRM.** Check it before offering a download and raise `DRMMediaError`.
- **Run blocking work off the loop** (`asyncio.to_thread`, or `create_subprocess_exec`), and
  forward progress through the `progress` callback so the queue can throttle edits.
- **Write only inside `dest_dir`.** Storage gives each job its own directory; that is what
  stops two users colliding on one filename.

For subprocess-driven engines, follow `gallerydl_engine.py`: drive the documented CLI, parse
`-J` JSON, and map exit codes/stderr onto typed errors. Its comment explains why — the Python
API of such tools is not a stable contract, while CLI flags are.

## Level 3 — a new delivery path

`senders.py` holds the ceilings (`BOT_API_SEND_LIMIT`, `LOCAL_API_SEND_LIMIT`,
`MTPROTO_SEND_LIMIT`) and `bigfiles.py` the fallback split. A new route must declare a real
byte ceiling and be *probed*, not assumed: `GrabberService.probe_senders()` runs at startup
and `choose_route()` only prefers what answered.
