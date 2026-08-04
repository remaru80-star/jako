import asyncio
import logging
import os
import shutil
import time

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

import config
import database as db
import nyaa_client
import matcher
import downloader
import encoder

logger = logging.getLogger(__name__)

# Single shared queue: the bot processes one nyaa link at a time. Anything else
# that arrives while a download is in progress just waits its turn here.
_QUEUE: asyncio.Queue = asyncio.Queue()


async def _is_source_channel(_, __, message: Message) -> bool:
    settings = await db.get_settings()
    source_id = settings.get("source_channel_id")
    return source_id is not None and message.chat.id == source_id


source_channel_filter = filters.create(_is_source_channel)


def register_handlers(app: Client):

    @app.on_message(source_channel_filter)
    async def on_source_message(client: Client, message: Message):
        nyaa_id = nyaa_client.extract_nyaa_id_from_message(message)
        if not nyaa_id:
            return
        await _QUEUE.put((client, message, nyaa_id))
        logger.info("Queued nyaa id %s (queue size now %d)", nyaa_id, _QUEUE.qsize())


async def worker():
    """Background task, started once at bot startup. Pulls one item at a time so
    only a single download/upload runs at any given moment."""
    logger.info("Queue worker started")
    while True:
        client, message, nyaa_id = await _QUEUE.get()
        try:
            await _process_one(client, message, nyaa_id)
        except Exception:
            logger.exception("Unhandled error while processing %s", nyaa_id)
        finally:
            _QUEUE.task_done()


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _progress_bar(fraction: float, width: int = 20) -> str:
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


# Minimum real time between status message edits. Telegram will flood-wait an
# account that edits the same message too often, and — critically — pyrogram
# awaits the progress callback for every chunk sent/received during a
# download or upload. If that callback blocks on a slow or flood-limited
# edit_text() call, the actual transfer stalls right along with it, which is
# what caused uploads to look "stuck" near completion while the last-shown
# speed still looked healthy (it reflected the last chunk that got through,
# not the time spent blocked afterwards).
_EDIT_INTERVAL = 8  # seconds


def _new_edit_gate() -> dict:
    return {"time": 0.0, "percent": -1, "bytes": 0, "busy": False, "cooldown_until": 0.0}


def _schedule_edit(gate: dict, msg: Message, text: str):
    """Fire a status edit in the background instead of awaiting it inline.

    Only one edit per gate is ever in flight, and a FloodWait pauses further
    edits until it expires instead of letting pyrogram's automatic retry
    block whatever coroutine happens to be awaiting this callback.
    """
    now = time.time()
    if gate["busy"] or now < gate["cooldown_until"]:
        return
    gate["busy"] = True

    async def _run():
        try:
            await msg.edit_text(text)
        except FloodWait as e:
            gate["cooldown_until"] = time.time() + e.value
        except Exception as e:
            logger.warning("Status edit failed: %s", e)
        finally:
            gate["busy"] = False

    asyncio.create_task(_run())


async def _safe_edit(msg: Message, text: str):
    """For the handful of one-off status edits (not per-chunk progress) that
    we do want to actually happen — retries once after a flood wait instead
    of losing the update or crashing the whole item."""
    try:
        await msg.edit_text(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await msg.edit_text(text)
        except Exception as e2:
            logger.warning("Status edit failed after flood wait: %s", e2)
    except Exception as e:
        logger.warning("Status edit failed: %s", e)


async def _process_one(client: Client, message: Message, nyaa_id: str):
    try:
        html = await nyaa_client.fetch_page(nyaa_id)
    except Exception as e:
        logger.warning("Failed to fetch nyaa page %s: %s", nyaa_id, e)
        return

    info = nyaa_client.parse_torrent_page(html, nyaa_id)

    if not info["is_dual_audio_english"]:
        logger.info(
            "Skipping %s: audio check failed (declared=%s, langs=%s)",
            nyaa_id, info["audio_declared_count"], info["audio_languages"],
        )
        return

    batch = await db.find_batch(info["anilist_id"], info["mal_id"])
    if not batch:
        logger.info(
            "Skipping %s: no matching batch (anilist_id=%s, mal_id=%s)",
            nyaa_id, info["anilist_id"], info["mal_id"],
        )
        return

    if not batch.get("searching", False):
        logger.info(
            "Skipping %s: matched batch %s but searching=False (anilist_id=%s, mal_id=%s)",
            nyaa_id, batch["_id"], info["anilist_id"], info["mal_id"],
        )
        return

    season, episode = matcher.extract_season_episode(info["title"])

    if await db.already_processed(nyaa_id, episode):
        logger.info("Skipping %s episode %s: already processed", nyaa_id, episode)
        return

    settings = await db.get_settings()
    backup_channel_id = settings.get("backup_channel_id")
    if not backup_channel_id:
        logger.error(
            "Matched a batch for %s but no backup channel is set (use /set_backup)", nyaa_id
        )
        return

    season_label = f"{season:02d}"
    episode_label = f"{episode:02d}" if episode is not None else "??"
    title_line = (
        f"**{batch.get('english_title', 'Unknown')}**\n"
        f"Season {season_label} · Episode {episode_label}\n"
        f"Audio: {', '.join(info['audio_languages'])}"
    )
    status_msg = await client.send_message(backup_channel_id, f"{title_line}\n\nQueued for download...")

    last_edit = _new_edit_gate()

    async def on_progress(status: dict):
        now = time.time()
        percent = int(status["progress"] * 100)
        if status["stage"] == "metadata":
            if now - last_edit["time"] < _EDIT_INTERVAL:
                return
            text = f"{title_line}\n\nFetching torrent metadata..."
        else:
            # Purely time-gated (not "same percent" gated) — on a fast download
            # the percent changes on nearly every tick, and gating on percent
            # alone let edits through far more often than intended.
            if percent == last_edit["percent"] or now - last_edit["time"] < _EDIT_INTERVAL:
                return
            bar = _progress_bar(status["progress"])
            speed = _format_bytes(status["download_rate"]) + "/s"
            total = _format_bytes(status["total_size"])
            text = (
                f"{title_line}\n\n"
                f"Downloading: [{bar}] {percent}%\n"
                f"{speed} · {total}"
            )
        last_edit["time"] = now
        last_edit["percent"] = percent
        _schedule_edit(last_edit, status_msg, text)

    file_path = None
    try:
        file_path = await downloader.download(
            info["magnet_link"], info["torrent_link"], dest_subdir=nyaa_id,
            progress_cb=on_progress,
        )

        # Download is done, but ffmpeg's two-pass encode for the first variant can
        # take many minutes with zero feedback of its own (on_variant_done only
        # fires once a variant is fully finished). Without this edit, the message
        # is left showing the last "Downloading: 100% · 0.0 B/s" line the whole
        # time it's encoding, which looks identical to a hung download.
        await _safe_edit(status_msg, f"{title_line}\n\nDownload complete. Starting encode...")

        dest_dir = os.path.join(config.DOWNLOAD_DIR, nyaa_id)
        last_message_link = None

        async def on_variant_start(label: str):
            await _safe_edit(status_msg, f"{title_line}\n\nEncoding {label}...")

        async def on_variant_done(label: str, encoded_path: str):
            nonlocal last_message_link
            await _safe_edit(status_msg, f"{title_line}\n\nEncoded {label}. Uploading...")

            upload_last_edit = _new_edit_gate()

            async def on_upload_progress(current: int, total: int):
                now = time.time()
                percent = int(current / total * 100) if total else 0
                if percent == upload_last_edit["percent"] or now - upload_last_edit["time"] < _EDIT_INTERVAL:
                    return

                elapsed = now - upload_last_edit["time"] if upload_last_edit["time"] else 0
                byte_delta = current - upload_last_edit["bytes"]
                speed = byte_delta / elapsed if elapsed > 0 else 0

                upload_last_edit["time"] = now
                upload_last_edit["percent"] = percent
                upload_last_edit["bytes"] = current

                bar = _progress_bar(current / total if total else 0)
                text = (
                    f"{title_line}\n\n"
                    f"Uploading {label}: [{bar}] {percent}%\n"
                    f"{_format_bytes(current)} / {_format_bytes(total)} · {_format_bytes(speed)}/s"
                )
                _schedule_edit(upload_last_edit, status_msg, text)

            # The actual upload must not be silently dropped, so unlike the
            # progress-bar edits above this one really is retried on FloodWait
            # rather than skipped.
            for attempt in range(4):
                try:
                    sent = await client.send_document(
                        chat_id=backup_channel_id,
                        document=encoded_path,
                        caption=f"{title_line}\nQuality: {label}",
                        progress=on_upload_progress,
                    )
                    break
                except FloodWait as e:
                    logger.warning(
                        "send_document flood-waited %ss (%s, attempt %d)", e.value, label, attempt + 1
                    )
                    await asyncio.sleep(e.value)
            else:
                raise RuntimeError(f"send_document kept flood-waiting for {label}")
            last_message_link = f"https://t.me/c/{str(backup_channel_id)[4:]}/{sent.id}"

            # Free this variant's disk space as soon as it's uploaded rather than
            # waiting on the remaining variants or the final cleanup.
            try:
                os.remove(encoded_path)
            except OSError:
                pass

        variants_done = await encoder.encode_all(
            file_path, output_dir=dest_dir, file_stem=nyaa_id,
            on_variant_start=on_variant_start,
            on_variant_done=on_variant_done,
        )

        if not variants_done:
            raise RuntimeError("No quality variants were encoded (source resolution too low?)")

        await db.mark_processed(nyaa_id, episode, batch["_id"], last_message_link)
        if episode is not None:
            await db.update_last_uploaded_episode(batch["_id"], episode)

        await _safe_edit(status_msg, f"{title_line}\n\nDone.")

    except Exception as e:
        logger.exception("Failed processing %s", nyaa_id)
        await _safe_edit(status_msg, f"{title_line}\n\nFailed: {e}")
    finally:
        # Clean up the per-item download directory unconditionally. A failed/timed-out
        # download or encode can leave partial data behind under DOWNLOAD_DIR/<nyaa_id>/
        # even when file_path was never set, so we can't rely on file_path here.
        dest_dir = os.path.join(config.DOWNLOAD_DIR, nyaa_id)
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)