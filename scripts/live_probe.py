#!/usr/bin/env python3
"""Live probe: run the real engines against public, permitted URLs and print evidence.

Usage:
    python scripts/live_probe.py                 # all default targets
    python scripts/live_probe.py --download      # also download the smallest variant
    python scripts/live_probe.py --url <link>    # probe a single URL

Default targets are openly licensed or public sample media (Wikimedia Commons,
Vimeo staff pick, YouTube Creative-Commons upload, direct .mp4/.jpg files).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_bot.config import Settings, load_dotenv, load_settings  # noqa: E402
from media_bot.errors import GrabberError, describe  # noqa: E402
from media_bot.extractors.registry import build_registry  # noqa: E402
from media_bot.extractors.base import Want  # noqa: E402
from media_bot.logging_setup import setup_logging  # noqa: E402
from media_bot.security import check_url  # noqa: E402

#: Every target here was fetched and verified to exist before being listed. Site pages
#: that need login (Vimeo, Instagram, X) are intentionally absent: they are covered by
#: the offline unit tests and by the cookies path, not by anonymous live probes.
DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("youtube-cc", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("direct-mp4", "https://download.samplelib.com/mp4/sample-5s.mp4"),
    ("direct-jpg", "https://picsum.photos/2000/1200"),
    ("commons-hires-jpg",
     "https://upload.wikimedia.org/wikipedia/commons/3/35/"
     "%22A_sure_horse_for_the_first_money%22_LCCN2002695764-restored.jpg"),
]


async def probe(settings: Settings, name: str, url: str, *, do_download: bool) -> dict[str, object]:
    registry = build_registry(settings)
    safe = check_url(url)
    bundle = None
    engine_id = ""
    error = ""
    try:
        bundle = await registry.extract(safe, want=Want.AUTO)
        engine_id = bundle.engine
    except GrabberError as exc:
        error = f"{describe(exc)[0]}: {exc}"
    except Exception as exc:  # pragma: no cover
        error = f"{type(exc).__name__}: {exc}"

    row: dict[str, object] = {"name": name, "url": url, "engine": engine_id, "error": error}
    if bundle is None:
        return row
    row.update({
        "platform": bundle.platform,
        "kind": bundle.kind.value,
        "title": (bundle.title or "")[:60],
        "duration": bundle.duration,
        "count": bundle.count,
        "variants": [(v.token, v.label, v.format_spec, v.size_text(), v.merge) for v in bundle.variants],
    })
    if do_download and bundle.variants:
        smallest = min(
            (v for v in bundle.variants if v.filesize), key=lambda v: v.filesize, default=bundle.variants[0]
        )
        try:
            dest = Path(settings.downloads_dir) / f"probe-{name}"
            dest.mkdir(parents=True, exist_ok=True)
            result = await registry.download(bundle, smallest, dest, progress=None)
            row["downloaded"] = [(p.name, p.stat().st_size) for p in result.files][:5]
            row["bytes"] = result.bytes_total
        except Exception as exc:
            row["download_error"] = f"{describe(exc)[0]}: {exc}"
    return row


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe engines against public URLs.")
    parser.add_argument("--url", action="append", default=[], help="extra URL(s) to probe")
    parser.add_argument("--download", action="store_true", help="also download smallest variant")
    parser.add_argument("--level", default="WARNING")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings()
    setup_logging(settings.data_dir, args.level)

    targets = list(DEFAULT_TARGETS) + [(f"custom-{i}", url) for i, url in enumerate(args.url)]
    rows = []
    for name, url in targets:
        row = await probe(settings, name, url, do_download=args.download)
        rows.append(row)
        status = "FAIL" if row.get("error") else "OK"
        print(f"[{status}] {row['name']:14s} engine={row.get('engine') or '-':10s} "
              f"platform={row.get('platform', '-'):18s} kind={row.get('kind', '-'):6s} "
              f"variants={len(row.get('variants', []) or [])} {row.get('error', '')}")
        for token, label, spec, size, merge in (row.get("variants") or [])[:6]:
            print(f"        {token:14s} {label:32s} spec={spec:14s} {size:14s} merge={merge}")
        if row.get("downloaded"):
            print(f"        downloaded {row['downloaded']} ({row.get('bytes')} bytes)")
    ok = sum(1 for row in rows if not row.get("error"))
    print(f"\n{ok}/{len(rows)} targets extracted successfully (download attempted: {args.download})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
