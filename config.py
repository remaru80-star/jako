import asyncio
import json
import logging
import os

import config

logger = logging.getLogger(__name__)

# (label, target_height, target_size_mb) — target size is roughly the midpoint of
# the requested band: 480p 50-150MB, 720p 100-250MB, 1080p 200-400MB. Two-pass
# bitrate targeting keeps the actual output close to this, but very short or very
# long episodes can still land near the edges of the band.
_VARIANTS = [
    ("480p", 480, 100),
    ("720p", 720, 175),
    ("1080p", 1080, 300),
]


async def _ffprobe_json(args: list[str]) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-of", "json", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='ignore')[-1000:]}")
    return json.loads(stdout.decode())


async def probe_duration(input_path: str) -> float:
    data = await _ffprobe_json(["-show_entries", "format=duration", input_path])
    return float(data["format"]["duration"])


async def probe_video_height(input_path: str) -> int:
    data = await _ffprobe_json([
        "-select_streams", "v:0", "-show_entries", "stream=height", input_path,
    ])
    return int(data["streams"][0]["height"])


async def probe_audio_stream_count(input_path: str) -> int:
    data = await _ffprobe_json([
        "-select_streams", "a", "-show_entries", "stream=index", input_path,
    ])
    # Dual-audio releases have 2 audio streams; fall back to 1 so the bitrate
    # math below never divides by zero if ffprobe reports none for some reason.
    return len(data.get("streams", [])) or 1


def _video_kbps_for_target(target_size_mb: float, duration: float, audio_streams: int,
                            audio_kbps: int) -> int:
    """How much of the target file size is left for video once every audio
    stream (dual-audio = 2 tracks) has taken its share, spread over the runtime."""
    target_bits = target_size_mb * 8 * 1024 * 1024
    audio_bits = audio_kbps * 1000 * audio_streams * duration
    video_bits = max(target_bits - audio_bits, 0)
    # Floor so a very long episode never asks libx264 for an unusably tiny bitrate
    return max(int(video_bits / duration / 1000), 150)


async def _run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='ignore')[-2000:]}")


async def _encode_variant(input_path: str, output_dir: str, file_stem: str, height: int,
                           target_size_mb: float, duration: float, audio_streams: int) -> str:
    """Two-pass H.264 + AAC 2.0 encode, scaled to `height`p, sized at ~target_size_mb."""
    video_kbps = _video_kbps_for_target(
        target_size_mb, duration, audio_streams, config.FFMPEG_AUDIO_BITRATE_KBPS,
    )
    output_path = os.path.join(output_dir, f"{file_stem}.mp4")
    passlog = os.path.join(output_dir, f"{file_stem}_2pass")
    scale_filter = f"scale=-2:{height}"

    pass1 = [
        "-y", "-i", input_path,
        "-vf", scale_filter,
        "-c:v", "libx264", "-preset", config.FFMPEG_PRESET,
        "-b:v", f"{video_kbps}k", "-pix_fmt", "yuv420p",
        "-pass", "1", "-passlogfile", passlog,
        "-an", "-f", "mp4", os.devnull,
    ]
    pass2 = [
        "-y", "-i", input_path,
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", scale_filter,
        "-c:v", "libx264", "-preset", config.FFMPEG_PRESET,
        "-b:v", f"{video_kbps}k", "-pix_fmt", "yuv420p",
        "-pass", "2", "-passlogfile", passlog,
        "-c:a", "aac", "-b:a", f"{config.FFMPEG_AUDIO_BITRATE_KBPS}k", "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]
    await _run_ffmpeg(pass1)
    await _run_ffmpeg(pass2)

    for ext in ("-0.log", "-0.log.mbtree"):
        try:
            os.remove(passlog + ext)
        except OSError:
            pass

    return output_path


async def encode_all(input_path: str, output_dir: str, file_stem: str,
                      on_variant_start=None, on_variant_done=None):
    """Encodes 480p -> 720p -> 1080p in that order, one at a time.

    Skips any variant whose target height exceeds the source's actual height
    (never upscales) and logs why. If `on_variant_start` is given, it's awaited
    as `await on_variant_start(label)` right before that variant's two-pass
    ffmpeg run begins — each pass over a full episode can take several minutes
    with no output of its own, so without this the caller has no way to show
    that anything is happening between "download complete" and the first
    upload. If `on_variant_done` is given, it's awaited as
    `await on_variant_done(label, output_path)` right after each variant
    finishes, so the caller can upload/send it immediately instead of waiting
    for every variant to be ready.

    Returns a list of (label, output_path) for whatever was actually encoded.
    """
    os.makedirs(output_dir, exist_ok=True)
    duration = await probe_duration(input_path)
    src_height = await probe_video_height(input_path)
    audio_streams = await probe_audio_stream_count(input_path)

    results = []
    for label, height, target_mb in _VARIANTS:
        if src_height < height:
            logger.info(
                "Skipping %s: source is %dp, won't upscale to %dp", label, src_height, height,
            )
            continue
        if on_variant_start:
            await on_variant_start(label)
        output_path = await _encode_variant(
            input_path, output_dir, f"{file_stem}_{label}", height, target_mb,
            duration, audio_streams,
        )
        results.append((label, output_path))
        if on_variant_done:
            await on_variant_done(label, output_path)
    return results
