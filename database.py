from motor.motor_asyncio import AsyncIOMotorClient
import config

_client = AsyncIOMotorClient(config.MONGO_URI)
db = _client[config.DB_NAME]

batches = db["batches"]                 # shared with AnimeDub Batch Manager bot
settings = db["nyaa_backup_settings"]   # single doc, _id="config"
channel_registry = db["channel_registry"]  # populated by /register, shared with SUBARU
processed = db["nyaa_backup_processed"]  # dedup log


SETTINGS_ID = "config"


async def get_settings() -> dict:
    doc = await settings.find_one({"_id": SETTINGS_ID})
    return doc or {"_id": SETTINGS_ID, "source_channel_id": None, "backup_channel_id": None}


async def set_source_channel(chat_id: int):
    await settings.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"source_channel_id": chat_id}},
        upsert=True,
    )


async def set_backup_channel(chat_id: int):
    await settings.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {"backup_channel_id": chat_id}},
        upsert=True,
    )


async def resolve_invite_link(invite_link: str):
    """Look up a channel previously bound via /register (see SUBARU channel_registry)."""
    doc = await channel_registry.find_one({"invite_link": invite_link})
    return doc["chat_id"] if doc else None


async def register_channel(invite_link: str, chat_id: int):
    await channel_registry.update_one(
        {"invite_link": invite_link},
        {"$set": {"chat_id": chat_id}},
        upsert=True,
    )


async def find_batch(anilist_id: int | None, mal_id: int | None):
    """Match a batch doc by anilist_id OR mal_id — either one alone is enough.
    Matches regardless of the batch's `searching` flag, and tolerates the id
    being stored as either an int or a string."""
    query_clauses = []
    if anilist_id:
        query_clauses.append({"anilist_id": {"$in": [anilist_id, str(anilist_id)]}})
    if mal_id:
        query_clauses.append({"mal_id": {"$in": [mal_id, str(mal_id)]}})
    if not query_clauses:
        return None
    return await batches.find_one({"$or": query_clauses})


async def already_processed(nyaa_id: str, episode: int | None) -> bool:
    key = f"{nyaa_id}:{episode}"
    return await processed.find_one({"_id": key}) is not None


async def mark_processed(nyaa_id: str, episode: int | None, batch_id, message_link: str):
    key = f"{nyaa_id}:{episode}"
    await processed.update_one(
        {"_id": key},
        {"$set": {
            "batch_id": batch_id,
            "episode": episode,
            "backup_message_link": message_link,
        }},
        upsert=True,
    )


async def update_last_uploaded_episode(batch_id, episode: int):
    """Only move the counter forward, never backward (in case of out-of-order posts)."""
    await batches.update_one(
        {"_id": batch_id, "last_uploaded_episode": {"$not": {"$gte": episode}}},
        {"$set": {"last_uploaded_episode": episode}},
    )