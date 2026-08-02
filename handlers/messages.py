import asyncio
import logging
import os
import shutil
import time

from pyrogram import Client, filters
from pyrogram.types import Message

import config
import database as db
import nyaa_client
import matcher
import downloader

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

    # Throttle edits so we don't hit Telegram's rate limit on fast-updating torrents
    last_edit = {"time": 0.0, "percent": -1}

    async def on_progress(status: dict):
        now = time.time()
        percent = int(status["progress"] * 100)
        if status["stage"] == "metadata":
            text = f"{title_line}\n\nFetching torrent metadata..."
            if now - last_edit["time"] < 5:
                return
        else:
            if percent == last_edit["percent"] and now - last_edit["time"] < 5:
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
        try:
            await status_msg.edit_text(text)
        except Exception as e:
            logger.warning("Download status edit failed at %d%%: %s", percent, e)

    file_path = None
    try:
        file_path = await downloader.download(
            info["magnet_link"], info["torrent_link"], dest_subdir=nyaa_id,
            progress_cb=on_progress,
        )

        upload_last_edit = {"time": 0.0, "percent": -1, "bytes": 0}

        async def on_upload_progress(current: int, total: int):
            now = time.time()
            percent = int(current / total * 100) if total else 0
            if percent == upload_last_edit["percent"] and now - upload_last_edit["time"] < 5:
                return

            elapsed = now - upload_last_edit["time"]
            byte_delta = current - upload_last_edit["bytes"]
            speed = byte_delta / elapsed if elapsed > 0 and upload_last_edit["time"] else 0

            upload_last_edit["time"] = now
            upload_last_edit["percent"] = percent
            upload_last_edit["bytes"] = current

            bar = _progress_bar(current / total if total else 0)
            text = (
                f"{title_line}\n\n"
                f"Uploading: [{bar}] {percent}%\n"
                f"{_format_bytes(current)} / {_format_bytes(total)} · {_format_bytes(speed)}/s"
            )
            try:
                await status_msg.edit_text(text)
            except Exception as e:
                logger.warning("Upload status edit failed at %d%%: %s", percent, e)

        sent = await client.send_document(
            chat_id=backup_channel_id,
            document=file_path,
            caption=title_line,
            progress=on_upload_progress,
        )

        message_link = f"https://t.me/c/{str(backup_channel_id)[4:]}/{sent.id}"
        await db.mark_processed(nyaa_id, episode, batch["_id"], message_link)
        if episode is not None:
            await db.update_last_uploaded_episode(batch["_id"], episode)

        await status_msg.edit_text(f"{title_line}\n\nDone.")

    except Exception as e:
        logger.exception("Failed processing %s", nyaa_id)
        try:
            await status_msg.edit_text(f"{title_line}\n\nFailed: {e}")
        except Exception:
            pass
    finally:
        # Clean up the per-item download directory unconditionally. A failed/timed-out
        # download can leave partial data behind under DOWNLOAD_DIR/<nyaa_id>/ even
        # when file_path was never set, so we can't rely on file_path here.
        dest_dir = os.path.join(config.DOWNLOAD_DIR, nyaa_id)
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)