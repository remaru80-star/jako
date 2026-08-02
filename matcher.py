import re

# --- Combined "S01E04" style ------------------------------------------------
# Handles releases like "... S01E04 ..." where season and episode are glued
# together with no boundary the separate regexes below can latch onto (the
# season regex needs a non-word char right after its digits, but "1E" is
# digit->letter with no boundary; same problem in reverse for the episode
# regex right before "E"). Checked first in extract_season_episode.
_SEASON_EPISODE_RE = re.compile(r"\bS(\d{1,2})E(\d{1,4})\b", re.IGNORECASE)

# --- Episode number -------------------------------------------------------
# Handles the common "[Group] Title - 05 (1080p)...", "... E05 ...", "... Ep 05 ..."
_EPISODE_PATTERNS = [
    re.compile(r"-\s*(\d{1,4})(?:v\d)?\s*(?:\(|\[|$)"),
    re.compile(r"\bE(?:p(?:isode)?)?\.?\s?(\d{1,4})\b", re.IGNORECASE),
]


def extract_episode(title: str) -> int | None:
    for pattern in _EPISODE_PATTERNS:
        m = pattern.search(title)
        if m:
            return int(m.group(1))
    return None


# --- Season number ---------------------------------------------------------
_ROMAN_MAP = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}

_SEASON_PATTERNS = [
    (re.compile(r"season\s*(\d{1,2})", re.IGNORECASE), lambda m: int(m.group(1))),
    (re.compile(r"\bS(\d{1,2})\b", re.IGNORECASE), lambda m: int(m.group(1))),
    (re.compile(r"(\d{1,2})(?:st|nd|rd|th)\s*season", re.IGNORECASE), lambda m: int(m.group(1))),
    (re.compile(r"\b(I{1,3}|IV|V|VI{0,3})\b\s*$"), lambda m: _ROMAN_MAP.get(m.group(1).upper())),
]


def extract_season(title: str) -> int:
    # Strip the episode/quality/hash tail so trailing roman numerals belonging to the
    # anime name (e.g. "Fate/Zero") aren't confused with a season marker after it.
    head = re.split(r"\s-\s*\d", title)[0]
    for pattern, extractor in _SEASON_PATTERNS:
        m = pattern.search(head)
        if m:
            value = extractor(m)
            if value:
                return value
    return 1


def extract_season_episode(title: str):
    m = _SEASON_EPISODE_RE.search(title)
    if m:
        return int(m.group(1)), int(m.group(2))
    return extract_season(title), extract_episode(title)