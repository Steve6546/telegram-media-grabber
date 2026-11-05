# Large files in Telegram

## The numbers, from the Bot API docs

| Direction | Hosted Bot API | Local Bot API Server | MTProto |
|---|---|---|---|
| Bot sends a file | **50 MB** | **2 GB** | **2 GB** (4 GB with Premium) |
| Bot downloads a file (`getFile`) | **20 MB** | **2 GB** | **2 GB** |

These are hard caps of the transport, not of this project. `media_bot/senders.py` encodes
them as constants and refuses *before* uploading, so a user gets an explanation instead of
a spinner that dies.

## The four routes

`GrabberService.choose_route(size)` picks the first that is genuinely available:

1. **`bot_api`** — size ≤ 50 MB. Default, no infrastructure.
2. **`local_api`** — chosen only if a Local Bot API Server answered `getMe` at startup
   (`probe_senders()`). Configuration alone does not qualify it: a wrong URL must degrade,
   not pretend.
3. **`mtproto`** — needs `TG_API_ID`, `TG_API_HASH`, an authorised Telethon session, and
   the `mtproto` extra.
4. **`split`** — FFmpeg `-c copy -f segment` into 45 MB parts, sent as documents. No
   re-encode, so no quality loss, but the user receives several files.

If `SEND_MODE=bot_api` pins route 1 and the file is bigger, the answer is `blocked` with
the reason.

## Route 2: Local Bot API Server

```bash
docker compose up -d bot-api        # see docker-compose.yml
```

Manual: build or pull [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) (the
`aiogram/telegram-bot-api` image works), start it with `--local`, and point
`LOCAL_API_URL` at it. It needs your own `API_ID`/`API_HASH` from my.telegram.org.

The catch that breaks most setups: in local mode the bot sends a **server-side path**, not
bytes. So both processes must see the same filesystem.

- Same machine or same container volume with identical paths → nothing to configure.
- Different paths → set both roots and the mapping is done by
  `aiogram.client.telegram.SimpleFilesPathWrapper`:

```ini
LOCAL_API_URL=http://bot-api:8081
LOCAL_FILES_ROOT=/data     # as the API server sees it
BOT_FILES_ROOT=/data       # as the bot sees it
```

`DATA_DIR` must live under the mapped root, otherwise the server cannot open the file.

## Route 3: MTProto, and when it is the wrong answer

MTProto authenticates a **user account**, not the bot. Consequences to weigh before
turning it on:

- the file appears sent **by that account**;
- that account must be able to see the recipient — your own saved messages and users who
  also message it. It cannot deliver to arbitrary bot users;
- a leaked session is worse than a leaked bot token.

```bash
pip install -e ".[mtproto]"
grabber mtproto-login          # interactive: phone, code, optional 2FA
```

The session file is written under `data/` and is git-ignored.

## Route 4: splitting

Automatic when FFmpeg exists and nothing else can carry the file. The caption states how
many parts were sent and why, so nobody mistakes it for one file.

## Downloading into Telegram (`getFile`)

20 MB for bots. `telegram_media.py` checks the announced size first and refuses with a
concrete message rather than letting Telegram fail mid-download.

## Tuning

```ini
MAX_FILE_MB=2000            # project-level hard stop, independent of the transport
TEMP_TTL_MINUTES=90         # job directories are purged after this
MAX_CONCURRENT=3            # simultaneous downloads
```

Disk usage grows with `MAX_CONCURRENT × MAX_FILE_MB`. Keep `data/` on a volume you monitor.
