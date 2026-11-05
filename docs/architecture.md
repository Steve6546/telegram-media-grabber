# Architecture

Interface is Telegram only. Everything below the handlers is Telegram-agnostic, so an
engine never imports a keyboard and a handler never shells out to yt-dlp.

```
update ─▶ bot/handlers.py ─▶ service.py ─▶ registry.py ─▶ extractors/*
                 │                │              │
                 │                │              ├─ ytdlp_engine    (video/audio/live/playlist)
                 │                │              ├─ gallerydl_engine(images, albums)
                 │                │              └─ direct_url      (bare file links)
                 │                ├─ queue.py    (global + per-user semaphores, cancel)
                 │                ├─ storage.py  (one directory per job, dedupe, TTL purge)
                 │                ├─ auth.py     (Netscape parsing, Fernet cookie vault)
                 │                └─ processing/ (ffprobe verification, ffmpeg ops)
                 └─ keyboards.py ─▶ senders.py / mtproto.py / bigfiles.py
```

## Contracts

`extractors/base.py` defines the three types every engine speaks:

- **`Bundle`** — what analysis produced: platform label, engine id, title, thumbnail,
  duration, count, `variants`, `items`, plus `needs_auth` / `has_drm` / `live` flags.
  Serialisable to a dict so it survives in SQLite between the two user turns.
- **`Variant`** — one honest choice. `token` is what travels inside `callback_data`,
  `format_spec` is what the engine downloads with, `filesize` is reported or estimated,
  and `merge` says FFmpeg must join separate audio and video.
- **`DownloadResult`** — files written, total bytes, which engine wrote them, and `info`
  filled by ffprobe with what actually arrived.

## Routing

`platform_detector.detect(url)` maps a host to a `Platform` (32 named entries plus a
generic fallback). `engine_order()` turns that plus the user's appetite (`Want`) into a
list of engine ids. `Registry.candidates()` filters it through each engine's own
`supports()`, so an engine can veto a URL it cannot serve.

`Registry.extract()` walks that list:

- `DRMMediaError` aborts immediately — no second engine gets a chance to route around
  encryption.
- other `GrabberError`s are collected; the most informative one wins
  (auth → rate-limit → blocked host → unsupported → timeout → network → no-media).
- a hard crash inside an engine is logged with traceback and treated as a failure of
  that engine, not of the bot.

## Adding a platform

Almost always you add nothing: yt-dlp and gallery-dl already know the site. Three cases:

1. **Better label or different engine preference** — add or edit one `Platform(...)` entry
   in `platform_detector.py`. That is a data change, no logic.
2. **A media kind no engine handles** — add a method to the closest engine, or a new
   `Extractor` subclass registered in `extractors/registry.py:build_registry()`. Handlers,
   queue, storage and senders need no edit.
3. **A new kind of choice in the UI** — extend `quality.build_variants()` so the tokens
   and labels are produced from what the engine reported. Never hand-write buttons.

## State

SQLite (`db.py`) holds four things on purpose: `jobs` for `/tasks` and audit, `dedupe` so
the same URL plus variant within `DEDUPE_TTL_MINUTES` is not downloaded twice, `bundles`
so an inline button can carry a 12-character token instead of a 64-byte-limit-breaking
payload, and `cookie_files` for metadata only — cookie bytes live on disk, encrypted.

## Two rules this codebase enforces

1. **No dead UI.** Every button routes to code that runs. If a capability is unavailable
   (no FFmpeg, no cookies, no local API server), the message says which one and what it
   would have unlocked — a test asserts no placeholder strings leak into keyboards.
2. **No silent failure.** Every exit path either delivers a file or raises a typed error
   that `describe()` turns into a code, a cause, and a next action.
