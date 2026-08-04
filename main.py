import asyncio
import logging

from pyrogram import Client, idle

import config
from handlers import commands, messages

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Client(
    "nyaa_backup_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    # Parallel connections used for file part upload/download. Default is 1
    # (fully sequential); raising this is what actually speeds up transfers.
    # tgcrypto (already installed) makes each of these connections cheaper
    # to encrypt, but this is the setting that gets them running in parallel.
    max_concurrent_transmissions=6,
    workers=8,
    # Default is 10s: any FloodWait longer than that gets raised to our code
    # instead of Pyrogram just sleeping it out. Our progress-edit calls can hit
    # FloodWaits in the 15-20s range under load, so raise this to let Pyrogram
    # absorb those quietly rather than bubbling up as caught exceptions.
    sleep_threshold=30,
)

commands.register_handlers(app)
messages.register_handlers(app)


async def main():
    await app.start()
    asyncio.create_task(messages.worker())
    logging.info("Nyaa Backup Bot started.")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
