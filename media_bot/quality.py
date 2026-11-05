"""Quality selection: turns a raw yt-dlp format table into honest user choices.

The invariant this module guarantees (and the previous generation of these bots broke):
every row shown to a user maps to a format id the engine really returned, with a real
or estimated byte size. There is no static 144p…4K ladder baked into the UI.
"""

from __future__ import annotations

from typing import Any, Iterable

from .extractors.base import MediaKind, Variant, human_size

HEIGHT_LABELS: dict[int, str] = {
    144: "144p",
    240: "240p",
    260: "260p",
    320: "320p",
    360: "360p",
    400: "400p",
    480: "480p",
    540: "540p",
    720: "720p HD",
    768: "768p",
    854: "nHD 854p",
    900: "900p",
    1080: "1080p Full HD",
    1280: "1280p",
    1440: "1440p 2K",
    2160: "2160p 4K",
    2880: "2880p",
    3840: "4K UHD",
    4320: "8K",
}

#: merge containers we can safely produce, in preference order
_SAFE_CONTAINERS = ("mp4", "mkv", "webm", "mov")


def quality_label(height: int | None, fps: float | None = None) -> str:
    if not height:
        return "جودة غير محددة"
    base = HEIGHT_LABELS.get(height, f"{height}p")
    if fps and fps > 30:
        return f"{base} {int(fps)}fps"
    return base


def _is_video(fmt: dict[str, Any]) -> bool:
    return (fmt.get("vcodec") or "none") != "none"


def _is_audio(fmt: dict[str, Any]) -> bool:
    return (fmt.get("acodec") or "none") != "none"


def _bitrate(fmt: dict[str, Any]) -> float:
    for key in ("tbr", "vbr", "abr"):
        value = fmt.get(key)
        if value:
            return float(value)
    return 0.0


def _size_bytes(fmt: dict[str, Any]) -> int | None:
    value = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(value) if value else None


def _estimate_bytes(fmt: dict[str, Any], duration: float | None) -> int | None:
    known = _size_bytes(fmt)
    if known:
        return known
    if not duration:
        return None
    kbps = _bitrate(fmt)
    if not kbps:
        return None
    return int(kbps * 1000 / 8 * duration)


def _combined_bytes(video: dict[str, Any], audio: dict[str, Any] | None,
                    duration: float | None) -> int | None:
    video_bytes = _estimate_bytes(video, duration)
    if video_bytes is None:
        return None
    if audio is None:
        return video_bytes
    audio_bytes = _estimate_bytes(audio, duration)
    return video_bytes + audio_bytes if audio_bytes is not None else video_bytes


def merge_container(formats: Iterable[dict[str, Any]], preferred: str = "mp4") -> str:
    """Container that can hold these codecs without a re-encode surprise."""
    exts = {fmt.get("ext") for fmt in formats if fmt.get("ext")}
    if not exts:
        return preferred
    if "mp4" in exts and preferred == "mp4":
        return "mp4"
    if "webm" in exts and preferred in {"webm", "auto"}:
        return "webm"
    for candidate in _SAFE_CONTAINERS:
        if candidate in exts:
            return candidate
    return "mkv"


def build_variants(
    formats: list[dict[str, Any]],
    *,
    duration: float | None = None,
    preferred_ext: str = "mp4",
    max_rows: int = 12,
) -> list[Variant]:
    """Return user-facing variants, highest first.

    Guarantees: no invented heights, one row per distinct height, and ``merge=True``
    whenever the produced file needs FFmpeg to join separate audio and video streams.
    """
    usable = [fmt for fmt in formats if fmt.get("format_id")]
    progressive = [fmt for fmt in usable if _is_video(fmt) and _is_audio(fmt)]
    video_only = [fmt for fmt in usable if _is_video(fmt) and not _is_audio(fmt)]
    audio_only = [fmt for fmt in usable if not _is_video(fmt) and _is_audio(fmt)]

    def best(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        return max(items, key=lambda f: (_bitrate(f), f.get("height") or 0), default=None)

    best_audio = best(audio_only)
    variants: list[Variant] = []
    seen_tokens: set[str] = set()

    def add(variant: Variant) -> None:
        token = variant.token
        index = 2
        while token in seen_tokens:
            token = f"{variant.token}~{index}"
            index += 1
        variant.token = token
        seen_tokens.add(token)
        variants.append(variant)

    # --- heights present in the source, merged from either stream style ---
    heights: dict[int | None, dict[str, Any]] = {}
    for fmt in progressive + video_only:
        height = fmt.get("height") or None
        current = heights.get(height)
        if current is None or _score(fmt, height) > _score(current, height):
            heights[height] = fmt

    for height in sorted([h for h in heights if h], reverse=True)[:max_rows]:
        fmt = heights[height]
        both = _is_audio(fmt)
        needs_audio = not both and best_audio is not None
        label = quality_label(height, fmt.get("fps"))
        ext = fmt.get("ext") or preferred_ext
        token_ext = ext if ext != preferred_ext else ""
        filesize = (_size_bytes(fmt) if both else None) or _combined_bytes(
            fmt, best_audio if needs_audio else None, duration
        )
        if not both and best_audio is None:
            # Video-only source (silent timelapse, some CDN ladders): deliver it as-is
            # instead of asking FFmpeg to merge with an audio track that does not exist.
            label = f"{label} (بلا صوت)"
        spec = fmt["format_id"]
        if needs_audio:
            spec = f"{fmt['format_id']}+{best_audio['format_id']}"
        add(Variant(
            token=f"v{height}-{token_ext}" if token_ext else f"v{height}",
            label=label,
            kind=MediaKind.VIDEO,
            format_spec=spec,
            ext=merge_container([fmt, best_audio] if needs_audio else [fmt], preferred_ext),
            height=height,
            fps=fmt.get("fps"),
            vbr=fmt.get("vbr"),
            abr=best_audio.get("abr") if needs_audio else fmt.get("abr"),
            filesize=filesize,
            merge=needs_audio,
            codec=_codec_name(fmt.get("vcodec")),
            note=("" if both else "يُدمج الصوت تلقائياً" if needs_audio else "المصدر بلا مسار صوتي"),
        ))

    # --- absolute best available ---
    top = max([f for f in heights.values() if f.get("height")], key=lambda f: f["height"], default=None)
    if top:
        add(Variant(
            token="best",
            label=f"أعلى جودة متاحة ({quality_label(top.get('height'), top.get('fps'))})",
            kind=MediaKind.VIDEO,
            format_spec="bv*+ba/b",
            ext=merge_container(usable, preferred_ext),
            height=top.get("height"),
            filesize=None,
            merge=True,
            note="يختار المحرك أفضل فيديو وأفضل صوت ثم يدمجهما",
        ))

    # --- silent video (useful for memes/loops, saves bandwidth) ---
    # Skipped when the source has no audio at all: every row above is already silent, and a
    # second identical button is the kind of dead UI this project is meant to avoid.
    silent = best(video_only) if best_audio else None
    if silent:
        add(Variant(
            token="silent",
            label=f"فيديو بدون صوت ({quality_label(silent.get('height'), silent.get('fps'))})",
            kind=MediaKind.VIDEO,
            format_spec=silent["format_id"],
            ext=silent.get("ext") or preferred_ext,
            height=silent.get("height"),
            filesize=_size_bytes(silent),
            merge=False,
            note="بلا مسار صوتي",
        ))

    # --- audio only ---
    if best_audio:
        add(Variant(
            token="audio-best",
            label=f"صوت فقط ({_codec_name(best_audio.get('acodec')) or best_audio.get('ext','')})",
            kind=MediaKind.AUDIO,
            format_spec=best_audio["format_id"],
            ext=best_audio.get("ext") or "m4a",
            abr=best_audio.get("abr") or _bitrate(best_audio),
            filesize=_size_bytes(best_audio) or _estimate_bytes(best_audio, duration),
            merge=False,
            note="بدون فيديو",
        ))
        grouped: dict[tuple[str, int], dict[str, Any]] = {}
        for fmt in audio_only:
            key = (fmt.get("ext") or "?", int(fmt.get("abr") or _bitrate(fmt) or 0))
            current = grouped.get(key)
            if current is None or (_size_bytes(fmt) or 0) > (_size_bytes(current) or 0):
                grouped[key] = fmt
        for (ext, abr), fmt in sorted(grouped.items(), key=lambda kv: -kv[0][1])[:4]:
            if fmt is best_audio:
                continue
            add(Variant(
                token=f"audio-{ext}-{abr or 'x'}",
                label=f"صوت {ext.upper()}" + (f" {abr}k" if abr else ""),
                kind=MediaKind.AUDIO,
                format_spec=fmt["format_id"],
                ext=ext,
                abr=abr or None,
                filesize=_size_bytes(fmt) or _estimate_bytes(fmt, duration),
                merge=False,
                note="بدون فيديو",
            ))

    # --- image/still fallbacks some platforms expose ---
    for fmt in usable:
        if not _is_video(fmt) and not _is_audio(fmt) and fmt.get("ext") in {"jpg", "png", "webp"}:
            add(Variant(
                token=f"still-{fmt['format_id']}",
                label="الصورة المصغرة/الغلاف",
                kind=MediaKind.IMAGE,
                format_spec=fmt["format_id"],
                ext=fmt["ext"],
                filesize=_size_bytes(fmt),
                merge=False,
            ))
            break
    return variants


def _score(fmt: dict[str, Any], height: int | None) -> float:
    """Rank formats within one height band: prefer high bitrate, then progressive."""
    return _bitrate(fmt) + (1 if fmt.get("height") == height else 0) + (
        0.5 if _is_audio(fmt) else 0
    )


def _codec_name(raw: str | None) -> str | None:
    if not raw or raw == "none":
        return None
    if raw.startswith("avc"):
        return "h264"
    if raw.startswith("hev") or raw.startswith("hvc"):
        return "hevc"
    if raw.startswith("vp9"):
        return "vp9"
    if raw.startswith("vp0") or raw.startswith("vp8"):
        return "vp9" if raw.startswith("vp09") else "vp8"
    if raw.startswith("av01"):
        return "av1"
    if raw.startswith("mp4a"):
        return "aac"
    if raw.startswith("opus"):
        return "opus"
    if raw.startswith("mp3"):
        return "mp3"
    return raw


def summarize(variants: list[Variant]) -> str:
    """Compact one-line list used in logs and the /doctor report."""
    return ", ".join(f"{v.label}≈{human_size(v.filesize)}" for v in variants[:12])


__all__ = ["build_variants", "quality_label", "merge_container", "summarize",
           "HEIGHT_LABELS", "_estimate_bytes"]
