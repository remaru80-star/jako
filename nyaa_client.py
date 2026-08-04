import asyncio
import re
import aiohttp
from bs4 import BeautifulSoup

import config

# Matches both the view page and the direct .torrent download link, since a message
# may only contain one of the two (e.g. only a "Torrent file" link, no "View site" link)
NYAA_URL_RE = re.compile(
    r"https?://(?:www\.)?nyaa\.si/(?:view/(\d+)|download/(\d+)\.torrent)",
    re.IGNORECASE,
)
ANILIST_RE = re.compile(r"anilist\.co/anime/(\d+)", re.IGNORECASE)
MAL_RE = re.compile(r"myanimelist\.net/anime/(\d+)", re.IGNORECASE)

# Matches lines like: "Audios (2): Japanese, AAC2.0 @ 192 kbps | English, AAC2.0 @ 192 kbps"
AUDIO_LINE_RE = re.compile(r"Audios?\s*\((\d+)\)\s*:\s*(.+)", re.IGNORECASE)


def extract_nyaa_id(text: str) -> str | None:
    m = NYAA_URL_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def extract_nyaa_id_from_message(message) -> str | None:
    """message.text/caption only contains the *visible* text. A markdown-style
    hyperlink like `[View site](https://nyaa.si/...)` renders as a `text_link`
    entity — the URL never appears in the plain text, only in
    message.entities/caption_entities. Check those too, plus any link preview
    Telegram generated for the message."""
    candidates = []

    for attr in ("text", "caption"):
        val = getattr(message, attr, None)
        if val:
            candidates.append(str(val))

    for attr in ("entities", "caption_entities"):
        entities = getattr(message, attr, None) or []
        for ent in entities:
            url = getattr(ent, "url", None)
            if url:
                candidates.append(url)

    web_page = getattr(message, "web_page", None)
    if web_page and getattr(web_page, "url", None):
        candidates.append(web_page.url)

    for text in candidates:
        nyaa_id = extract_nyaa_id(text)
        if nyaa_id:
            return nyaa_id
    return None


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


async def fetch_page(nyaa_id: str, retries: int = 3) -> str:
    url = f"{config.NYAA_BASE}/view/{nyaa_id}"
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            async with aiohttp.ClientSession(headers=_HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    resp.raise_for_status()
                    return await resp.text()
        except Exception as e:
            last_error = e
            if attempt < retries:
                await asyncio.sleep(2 * attempt)
    raise last_error


_MARKDOWN_CHARS_RE = re.compile(r"[`*_]+")
_AUDIO_SEPARATOR_RE = re.compile(r"[|│¦]")


def parse_audio_line(description_text: str):
    """Returns (declared_count, [language, ...]) or (None, []) if no Audios line found.

    Nyaa descriptions are raw markdown, not pre-rendered HTML, so backticks/asterisks
    used for bold/code styling show up literally in the text — strip those first.
    The separator between audio tracks is often the unicode │ character rather than
    a plain ASCII pipe, so both are accepted.
    """
    cleaned = _MARKDOWN_CHARS_RE.sub("", description_text)
    m = AUDIO_LINE_RE.search(cleaned)
    if not m:
        return None, []
    declared_count = int(m.group(1))
    rest_of_line = m.group(2).split("\n")[0]
    langs = []
    for part in _AUDIO_SEPARATOR_RE.split(rest_of_line):
        lang = part.strip().split(",")[0].strip()
        if lang:
            langs.append(lang)
    return declared_count, langs


def has_dual_audio_with_english(declared_count: int | None, langs: list[str]) -> bool:
    if declared_count != 2 or len(langs) != 2:
        return False
    lowered = [l.lower() for l in langs]
    return "english" in lowered and len(set(lowered)) == 2


def parse_torrent_page(html: str, nyaa_id: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h3.panel-title")
    title = title_el.get_text(strip=True) if title_el else ""

    desc_el = soup.select_one("#torrent-description")
    description_text = desc_el.get_text("\n", strip=True) if desc_el else ""

    torrent_link = None
    magnet_link = None
    for a in soup.select("a[href]"):
        href = a["href"]
        if href.startswith("/download/"):
            torrent_link = config.NYAA_BASE + href
        elif href.startswith("magnet:"):
            magnet_link = href

    anilist_m = ANILIST_RE.search(description_text) or ANILIST_RE.search(html)
    mal_m = MAL_RE.search(description_text) or MAL_RE.search(html)

    declared_count, langs = parse_audio_line(description_text)

    return {
        "nyaa_id": nyaa_id,
        "title": title,
        "description_text": description_text,
        "torrent_link": torrent_link,
        "magnet_link": magnet_link,
        "anilist_id": int(anilist_m.group(1)) if anilist_m else None,
        "mal_id": int(mal_m.group(1)) if mal_m else None,
        "audio_declared_count": declared_count,
        "audio_languages": langs,
        "is_dual_audio_english": has_dual_audio_with_english(declared_count, langs),
    }