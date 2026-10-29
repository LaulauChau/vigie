"""Reading RSS and Atom feeds with feedparser."""

import math
from datetime import UTC, datetime
from time import struct_time
from typing import TypeAlias

import feedparser

# feedparser has no type hints, so this is Any to mypy.
Entry: TypeAlias = feedparser.FeedParserDict


class NotAFeed(Exception):
    pass


def parse_feed(body: bytes) -> list[Entry]:
    """The feed's entries, or NotAFeed if the body is not RSS or Atom."""
    feed = feedparser.parse(body)
    # feedparser is lenient: `bozo` alone also flags minor issues in usable feeds,
    # and well-formed HTML is not bozo, it just has no feed version.
    if not feed.entries and (feed.bozo or not feed.version):
        reason = feed.get("bozo_exception") or "no RSS or Atom content"
        raise NotAFeed(f"not a feed: {reason}")
    entries: list[Entry] = feed.entries
    return entries


def entry_date(entry: Entry) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def is_podcast(entry: Entry) -> bool:
    """An entry with an audio enclosure is a podcast episode."""
    return any(
        str(enclosure.get("type", "")).startswith("audio/")
        for enclosure in entry.get("enclosures", [])
    )


def itunes_duration(entry: Entry) -> int | None:
    """Seconds from `itunes:duration`: either seconds, MM:SS or HH:MM:SS."""
    raw = str(entry.get("itunes_duration", "")).strip()
    try:
        parts = [float(p) for p in raw.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or not all(math.isfinite(p) for p in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return round(seconds)
