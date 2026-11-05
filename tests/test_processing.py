"""Real FFmpeg/ffprobe behaviour on generated media, not mocked assertions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from media_bot.bigfiles import can_split, split
from media_bot.errors import EngineError
from media_bot.processing.ffmpeg import FFmpeg
from media_bot.processing.probe import probe

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@pytest.fixture(scope="module")
def ffmpeg() -> FFmpeg:
    return FFmpeg()


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    """A 6-second 320x240 test clip with an audio track, made by FFmpeg itself."""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True, timeout=120,
    )
    return path


async def test_probe_reads_real_stream_facts(sample_video) -> None:
    info = await probe(sample_video)
    assert info.error is None
    assert (info.width, info.height) == (320, 240)
    assert info.vcodec == "h264"
    assert info.acodec in {"aac", "mp4"}
    assert 5 <= info.duration <= 7
    assert info.size_bytes == sample_video.stat().st_size
    assert "320x240" in info.label


async def test_probe_on_a_plain_image(tmp_path) -> None:
    jpg = tmp_path / "still.jpg"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=red:size=64x64", "-frames:v", "1", str(jpg)],
                   check=True, capture_output=True, timeout=60)
    info = await probe(jpg)
    assert info.width == 64 and info.height == 64
    assert info.error is None


async def test_probe_missing_ffprobe_degrades_to_size_only(sample_video, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    info = await probe(sample_video, ffprobe_path="ffprobe")
    assert info.error == "ffprobe غير متاح"
    assert info.size_bytes > 0


async def test_remux_changes_container_not_pixels(ffmpeg, sample_video, tmp_path) -> None:
    out = await ffmpeg.remux(sample_video, "mkv", out_dir=tmp_path)
    assert out.suffix == ".mkv"
    info = await probe(out)
    assert (info.width, info.height) == (320, 240)
    assert "matroska" in (info.container or "")


async def test_remux_rejects_nonsense_container(ffmpeg, sample_video) -> None:
    with pytest.raises(EngineError):
        await ffmpeg.remux(sample_video, "exe")


async def test_to_audio_drops_the_video_stream(ffmpeg, sample_video, tmp_path) -> None:
    out = await ffmpeg.to_audio(sample_video, "mp3", bitrate="96k", out_dir=tmp_path)
    assert out.suffix == ".mp3"
    info = await probe(out)
    assert info.vcodec is None
    assert info.acodec == "mp3"
    assert 5 <= info.duration <= 7


async def test_to_audio_rejects_unknown_format(ffmpeg, sample_video) -> None:
    with pytest.raises(EngineError):
        await ffmpeg.to_audio(sample_video, "xyz")


async def test_trim_shortens_the_clip(ffmpeg, sample_video, tmp_path) -> None:
    out = await ffmpeg.trim(sample_video, start=1, end=3, out_dir=tmp_path)
    info = await probe(out)
    assert 1 < (info.duration or 0) <= 3.5


async def test_trim_validates_range(ffmpeg, sample_video) -> None:
    with pytest.raises(EngineError):
        await ffmpeg.trim(sample_video, start=5, end=2)


async def test_shrink_reduces_height(ffmpeg, sample_video, tmp_path) -> None:
    out = await ffmpeg.shrink(sample_video, max_height=120, out_dir=tmp_path)
    info = await probe(out)
    assert info.height and info.height <= 120


@pytest.fixture(scope="module")
def big_video(tmp_path_factory) -> Path:
    """A ~4 MB clip, so a 1 MB chunk target really does produce several parts."""
    path = tmp_path_factory.mktemp("big") / "big.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "3M", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True, timeout=180,
    )
    return path


async def test_split_produces_under_target_parts(ffmpeg, big_video, tmp_path) -> None:
    parts = await split(big_video, ffmpeg=ffmpeg, chunk_mb=1, out_dir=tmp_path)
    assert len(parts) >= 2
    ceiling = 2 * 1024 * 1024
    assert all(part.stat().st_size <= ceiling for part in parts)
    assert sum(part.stat().st_size for part in parts) >= big_video.stat().st_size * 0.8


async def test_split_of_small_file_is_a_noop(ffmpeg, sample_video, tmp_path) -> None:
    assert await split(sample_video, ffmpeg=ffmpeg, chunk_mb=500, out_dir=tmp_path) == [sample_video]


async def test_split_refuses_non_media(ffmpeg, tmp_path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert can_split(pdf) is False
    with pytest.raises(EngineError):
        await split(pdf, ffmpeg=ffmpeg, chunk_mb=1)


async def test_split_without_ffmpeg_is_reported(sample_video, tmp_path) -> None:
    broken = FFmpeg(ffmpeg_path="definitely-not-installed-ffmpeg")
    assert broken.available is False
    with pytest.raises(EngineError) as caught:
        await split(sample_video, ffmpeg=broken, chunk_mb=1, out_dir=tmp_path)
    assert "FFmpeg" in str(caught.value)


async def test_run_reports_ffmpeg_failure_detail(ffmpeg, tmp_path) -> None:
    missing = tmp_path / "nope.mp4"
    with pytest.raises(EngineError) as caught:
        await ffmpeg.run(["-i", str(missing), "-c", "copy", str(tmp_path / "out.mp4")])
    assert caught.value.detail


def test_version_string_when_available(ffmpeg) -> None:
    assert "ffmpeg" in ffmpeg.version().lower() or "version" in ffmpeg.version().lower()
