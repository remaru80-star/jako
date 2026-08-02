import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Comma separated telegram user ids allowed to run admin commands
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

MONGO_URI = os.environ["MONGO_URI"]
# Same database the AnimeDub Batch Manager bot uses, so we can read `batches`
DB_NAME = os.environ.get("DB_NAME", "subaru")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/nyaa_downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# How long (seconds) to let libtorrent try before giving up on a torrent
TORRENT_TIMEOUT = int(os.environ.get("TORRENT_TIMEOUT", "3600"))

NYAA_BASE = "https://nyaa.si"
