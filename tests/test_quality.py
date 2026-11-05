"""Quality selection must never offer a resolution the source does not have."""

from __future__ import annotations

import pytest

from media_bot.extractors.base import MediaKind
from media_bot.quality import build_variants, merge_container, quality_label

YOUTUBE_LIKE = [
    {"format_id": "137", "ext": "mp4", "height": 1080, "fps": 30, "vcodec": "avc1.640028",
     "acodec": "none", "vbr": 1200, "filesize_approx": 41_943_040},
    {"format_id": "248", "ext": "webm", "height": 1080, "fps": 30, "vcodec": "vp9",
     "acodec": "none", "vbr": 1100, "filesize_approx": 38_000_000},
    {"format_id": "271", "ext": "webm", "height": 1440, "fps": 60, "vcodec": "vp9",
     "acodec": "none", "vbr": 3000},
    {"format_id": "315", "ext": "webm", "height": 2160, "fps": 60, "vcodec": "vp9",
     "acodec": "none", "vbr": 6000},
    {"format_id": "136", "ext": "mp4", "height": 720, "vcodec": "avc1.4d401f",
     "acodec": "none", "vbr": 700, "filesize_approx": 22_000_000},
    {"format_id": "134", "ext": "mp4", "height": 360, "vcodec": "avc1.4d401e",
     "acodec": "none", "vbr": 300, "filesize_approx": 9_000_000},
    {"format_id": "160", "ext": "mp4", "height": 144, "vcodec": "avc1.4d400c",
     "acodec": "none", "tbr": 100},
    {"format_id": "22", "ext": "mp4", "height": 720, "vcodec": "avc1.4d401f",
     "acodec": "mp4a.40.2", "tbr": 750, "filesize": 25_000_000},
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128,
     "filesize_approx": 4_800_000},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160,
     "filesize_approx": 6_000_000},
]


def heights(variants) -> list[int | None]:
    return [v.height for v in variants if v.kind is MediaKind.VIDEO]


def test_only_present_heights_are_offered() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    got = {h for h in heights(variants) if h}
    assert got == {2160, 1440, 1080, 720, 360, 144}
    # 480p and 240p are absent from the source and must not appear
    assert 480 not in got and 240 not in got


def test_ladder_rows_are_ordered_high_to_low() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    # The ladder (v<height> tokens) comes first; best/silent/audio are extra rows after it.
    ladder = [v.height for v in variants if v.token.startswith("v")]
    assert ladder == sorted(ladder, reverse=True)


def test_progressive_format_avoids_merge() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    by_height = {v.height: v for v in variants if v.kind is MediaKind.VIDEO and v.height}
    assert by_height[720].merge is False
    assert by_height[720].format_spec == "22"
    assert by_height[1080].merge is True
    assert "+" in by_height[1080].format_spec


def test_merged_size_adds_audio_and_video() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    v1080 = next(v for v in variants if v.height == 1080)
    # 41.9 MB video + 6 MB opus audio ≈ 45.7 MB
    assert v1080.filesize == pytest.approx(47_943_040, rel=0.01)


def test_best_variant_is_present_and_engine_picked() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    best = next(v for v in variants if v.token == "best")
    assert best.format_spec == "bv*+ba/b"
    assert best.merge is True
    assert best.height == 2160


def test_silent_and_audio_options_exist() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    assert any(v.token == "silent" for v in variants)
    audio = [v for v in variants if v.kind is MediaKind.AUDIO]
    assert audio and audio[0].token == "audio-best"
    assert audio[0].filesize


def test_tokens_are_unique_and_short_enough_for_callback_data() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    tokens = [v.token for v in variants]
    assert len(tokens) == len(set(tokens))
    for token in tokens:
        payload = f"q:{'a' * 12}:{token}".encode("utf-8")
        assert len(payload) <= 64


def test_empty_format_table_yields_no_variants() -> None:
    assert build_variants([], duration=10) == []


def test_audio_only_source_offers_audio() -> None:
    variants = build_variants([YOUTUBE_LIKE[8]], duration=100)
    assert [v.kind for v in variants] == [MediaKind.AUDIO]


def test_estimate_from_bitrate_when_size_missing() -> None:
    variants = build_variants(YOUTUBE_LIKE, duration=300)
    v2160 = next(v for v in variants if v.height == 2160)
    assert v2160.filesize and v2160.filesize > 10_000_000


def test_no_duration_keeps_size_unknown_rather_than_fictional() -> None:
    formats = [{"format_id": "1", "ext": "mp4", "height": 480, "vcodec": "avc1", "acodec": "none"}]
    variants = build_variants(formats, duration=None)
    assert variants[0].filesize is None


def test_label_text() -> None:
    assert quality_label(720) == "720p HD"
    assert quality_label(2160) == "2160p 4K"
    assert quality_label(1080, 60) == "1080p Full HD 60fps"
    assert quality_label(None) == "جودة غير محددة"


def test_merge_container_prefers_safe_choice() -> None:
    assert merge_container([{"ext": "webm"}], "mp4") == "webm"
    assert merge_container([{"ext": "mp4"}], "mp4") == "mp4"
    assert merge_container([], "mp4") == "mp4"
    assert merge_container([{"ext": "ts"}], "mp4") == "mkv"


def test_max_rows_caps_long_ladders() -> None:
    many = [{"format_id": f"f{i}", "ext": "mp4", "height": 100 + i * 10,
             "vcodec": "avc1", "acodec": "none", "vbr": 100 + i} for i in range(20)]
    variants = build_variants(many, duration=10, max_rows=6)
    ladder = [v for v in variants if v.token.startswith("v")]
    assert len(ladder) <= 6
    # no audio track exists, so no duplicate "silent" row either
    assert not any(v.token == "silent" for v in variants)
