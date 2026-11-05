"""Command line entrypoint for the grabber."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .auth import generate_key
from .config import ConfigError, load_dotenv, load_settings


def _load(args):
    load_dotenv(args.env or ".env")
    try:
        return load_settings(args.config)
    except ConfigError as exc:
        print(f"إعداد غير صالح: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


async def cmd_run(args) -> int:
    from .app import run_bot

    settings = _load(args)
    if not settings.bot_token:
        print("BOT_TOKEN مفقود: انسخ .env.example إلى .env وضع توكن @BotFather.", file=sys.stderr)
        return 2
    await run_bot(settings)
    return 0


async def cmd_doctor(args) -> int:
    from .doctor import report

    settings = _load(args)
    return await report(settings, check_token=args.check_token)


async def cmd_analyze(args) -> int:
    from .app import create_services
    from .extractors.base import Want
    from .security import check_url

    settings = _load(args)
    services = await create_services(settings, connect_mtproto=False)
    try:
        url = check_url(args.url)
        bundle = await services.service.analyze(url, user_id=0, want=Want(args.want))
        print(json.dumps({
            "platform": bundle.platform,
            "engine": bundle.engine,
            "kind": bundle.kind.value,
            "title": bundle.title,
            "duration": bundle.duration,
            "count": bundle.count,
            "live": bundle.live,
            "notes": bundle.notes,
            "variants": [
                {"token": v.token, "label": v.label, "ext": v.ext, "height": v.height,
                 "filesize": v.filesize, "merge": v.merge, "spec": v.format_spec}
                for v in bundle.variants
            ],
            "items": [{"name": i.name, "url": i.url} for i in bundle.items][:12],
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        services.db.close()


async def cmd_cleanup(args) -> int:
    from .app import create_services

    settings = _load(args)
    services = await create_services(settings, connect_mtproto=False)
    try:
        print(json.dumps(await services.service.maintenance(), ensure_ascii=False))
        return 0
    finally:
        services.db.close()


async def cmd_mtproto_login(args) -> int:
    from .mtproto import interactive_login

    settings = _load(args)
    if not settings.mtproto_configured:
        print("اضبط TG_API_ID و TG_API_HASH أولاً من my.telegram.org", file=sys.stderr)
        return 2
    return 0 if await interactive_login(settings) else 1


def cmd_keygen(args) -> int:
    print(generate_key())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grabber",
        description="Telegram media grabber: video, audio, images and albums from many sites.",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--env", default=None, help="path to .env file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the bot with long polling").set_defaults(func=cmd_run)
    doctor = sub.add_parser("doctor", help="report engines, ceilings and misconfiguration")
    doctor.add_argument("--check-token", action="store_true", help="call getMe to validate the token")
    doctor.set_defaults(func=cmd_doctor)

    analyze = sub.add_parser("analyze", help="analyse one URL and print the real variants")
    analyze.add_argument("url")
    analyze.add_argument("--want", default="auto", choices=["auto", "video", "audio", "image"])
    analyze.set_defaults(func=cmd_analyze)

    sub.add_parser("cleanup", help="delete expired temp files and finished job rows").set_defaults(
        func=cmd_cleanup)
    sub.add_parser("mtproto-login", help="authorize a Telethon session for 2 GB uploads").set_defaults(
        func=cmd_mtproto_login)
    sub.add_parser("keygen", help="print a fresh Fernet key for COOKIE_ENC_KEY").set_defaults(func=cmd_keygen)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - redirected streams
            pass
    args = build_parser().parse_args(argv)
    result = args.func(args)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


if __name__ == "__main__":
    raise SystemExit(main())
