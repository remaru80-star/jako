import asyncio
import os
import time

import aiohttp
import libtorrent as lt

import config
import nyaa_client


async def _get_torrent_file_bytes(torrent_link: str) -> bytes:
    async with aiohttp.ClientSession(headers=nyaa_client._HEADERS) as session:
        async with session.get(torrent_link, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return await resp.read()


def _blocking_download(atp_source: dict, save_path: str, timeout: int,
                        progress_cb=None) -> str:
    """Runs libtorrent's synchronous loop in a thread. Returns the path to the
    largest downloaded file (assumed to be the episode itself).

    progress_cb, if given, is a plain (non-async) callable invoked periodically
    with a dict: {stage, progress, download_rate, total_size, state}.
    """
    ses = lt.session()
    ses.listen_on(6881, 6891)

    params = {
        "save_path": save_path,
        "storage_mode": lt.storage_mode_t(2),
    }
    if "torrent_data" in atp_source:
        info = lt.torrent_info(lt.bdecode(atp_source["torrent_data"]))
        params["ti"] = info
        handle = ses.add_torrent(params)
    else:
        params["url"] = atp_source["magnet"]
        handle = lt.add_magnet_uri(ses, atp_source["magnet"], params)

    start = time.time()
    # Wait for metadata if we started from a magnet link only
    while not handle.has_metadata():
        if time.time() - start > timeout:
            raise TimeoutError("Timed out fetching torrent metadata")
        if progress_cb:
            progress_cb({"stage": "metadata", "progress": 0, "download_rate": 0,
                         "total_size": 0, "state": "fetching metadata"})
        time.sleep(1)

    while handle.status().state != lt.torrent_status.seeding:
        status = handle.status()
        if status.state == lt.torrent_status.finished:
            break
        if time.time() - start > timeout:
            raise TimeoutError("Timed out downloading torrent")
        if progress_cb:
            progress_cb({
                "stage": "downloading",
                "progress": status.progress,
                "download_rate": status.download_rate,
                "total_size": status.total_wanted,
                "state": str(status.state),
            })
        time.sleep(2)

    if progress_cb:
        progress_cb({"stage": "downloading", "progress": 1.0, "download_rate": 0,
                     "total_size": handle.status().total_wanted, "state": "finished"})

    info = handle.get_torrent_info() if hasattr(handle, "get_torrent_info") else handle.torrent_file()
    files = info.files()
    largest = max(range(files.num_files()), key=lambda i: files.file_size(i))
    file_path = os.path.join(save_path, files.file_path(largest))

    ses.remove_torrent(handle)
    return file_path


async def download(magnet_link: str | None, torrent_link: str | None, dest_subdir: str,
                    progress_cb=None) -> str:
    """progress_cb, if given, is an async callable: `await progress_cb(status_dict)`.
    It gets called from the libtorrent thread via run_coroutine_threadsafe, so it's
    safe to do async Telegram edits inside it."""
    save_path = os.path.join(config.DOWNLOAD_DIR, dest_subdir)
    os.makedirs(save_path, exist_ok=True)

    if magnet_link:
        source = {"magnet": magnet_link}
    elif torrent_link:
        torrent_bytes = await _get_torrent_file_bytes(torrent_link)
        source = {"torrent_data": torrent_bytes}
    else:
        raise ValueError("No magnet or torrent link provided")

    loop = asyncio.get_event_loop()

    def sync_progress_cb(status_dict):
        if progress_cb:
            asyncio.run_coroutine_threadsafe(progress_cb(status_dict), loop)

    file_path = await loop.run_in_executor(
        None, _blocking_download, source, save_path, config.TORRENT_TIMEOUT,
        sync_progress_cb if progress_cb else None,
    )
    return file_path