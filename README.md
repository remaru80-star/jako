# Nyaa Backup Bot

Standalone bot that watches a Telegram channel for nyaa.si torrent links, checks the
release for English dual-audio, matches it against a `batches` document from your
AnimeDub Batch Manager's MongoDB (shared DB, `batches` collection), extracts the
season/episode, downloads via libtorrent, and re-uploads to a backup channel.

## Flow

1. New message arrives in the configured **source channel**.
2. Bot extracts a `nyaa.si/view/<id>` link from the message text/caption.
3. Bot fetches that nyaa page and parses:
   - the `Audios (N): Lang1, ... | Lang2, ...` line from the torrent description
   - an AniList (`anilist.co/anime/<id>`) or MAL (`myanimelist.net/anime/<id>`) link, if present in the description
   - the `.torrent` download link and/or magnet link
4. Skips if audio isn't exactly 2 tracks with English as one of them.
5. Looks up `batches` for a doc with matching `anilist_id`/`mal_id` and `searching: true`.
6. Skips if no match.
7. Parses season/episode out of the release title (regex heuristic — same category
   of best-effort parsing as the season heuristic in AnimeDub Batch Manager).
8. Downloads the torrent via libtorrent, uploads the largest file in it to the
   **backup channel**, records the result, and bumps `last_uploaded_episode`.

Only one item downloads/uploads at a time — matching messages that arrive while
something is already in progress get queued (`handlers/messages.py`'s `worker()`
task, started in `main.py`) and processed in order, one after another.

## Setup

```
cp .env.example .env   # fill in API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS, MONGO_URI, DB_NAME
pip install -r requirements.txt
python main.py
```

`DB_NAME` should match the DB your AnimeDub Batch Manager bot uses, so this bot can
read the same `batches` collection.

## Commands (admin only, via `ADMIN_IDS`)

- `/set_source` — binds the channel to watch for torrent-link messages.
- `/set_backup` — binds the channel uploads get sent to.
- `/register` — run **inside** a channel to make it resolvable by invite link later.
- `/status` — shows currently bound source/backup channels.

Each of `/set_source` / `/set_backup` accepts, in this order:
1. Run the command directly inside the target channel.
2. Reply to a message forwarded from the target channel.
3. Pass the channel's numeric id as an argument.
4. Pass an invite link that was already bound via `/register`.

## Known caveats / things worth checking before relying on this

- **Audio parsing is text-based.** It reads nyaa's rendered description div for a line
  matching `Audios (N): ...`. If a release only links out to a separate "Full MediaInfo"
  page (as in your screenshot) instead of inlining the tech specs, this bot won't see it
  and will skip the release. Following that external link isn't implemented yet.
- **Season/episode parsing is a regex heuristic** on the release title, similar in spirit
  to (but simpler than) the AnimeDub Batch Manager's title-parsing fix. It won't handle
  every naming convention (e.g. batch/multi-episode releases, decimal episodes like
  `12.5`, or titles that put the episode number before a dash instead of after).
- **libtorrent has no seeding/ratio management** here — it downloads until finished and
  quits. If you want it to keep seeding back to the swarm, that needs to be added.
- **No file-size/duplicate-quality guard.** If two different releases for the same
  episode both pass the dual-audio-English check, both will be uploaded to the backup
  channel (only the DB's `last_uploaded_episode` counter is deduped by nyaa-id+episode,
  not by "is this actually a better/duplicate encode").
