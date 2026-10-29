"""`vigie check-sources`: fetch every Source once and report what came back."""

import re
from dataclasses import dataclass, replace
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from vigie.config import Config, HtmlIndexSource, Source
from vigie.feeds import NotAFeed, entry_date, parse_feed
from vigie.world import FetchError, World


@dataclass(frozen=True)
class SourceCheck:
    name: str
    type: str
    status: int | None = None
    # Feed Sources only.
    entries: int | None = None
    newest: date | None = None
    newest_age_days: int | None = None
    # HTML index Sources only: unique article links matching the link regex.
    links: int | None = None
    error: str | None = None


def check_sources(config: Config, world: World) -> list[SourceCheck]:
    rows = [_check(source, world) for source in config.sources]
    today = world.now().date()
    return [
        replace(row, newest_age_days=(today - row.newest).days) if row.newest else row
        for row in rows
    ]


COLUMNS = ["Source", "Type", "HTTP", "Entries", "Newest", "Links", "Error"]


def render_table(rows: list[SourceCheck]) -> str:
    cells = [COLUMNS] + [_cells(row) for row in rows]
    widths = [max(len(line[i]) for line in cells) for i in range(len(COLUMNS))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip() for line in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _cells(row: SourceCheck) -> list[str]:
    newest = "-"
    if row.newest is not None:
        newest = f"{row.newest.isoformat()} ({row.newest_age_days}d ago)"
    return [
        row.name,
        row.type,
        _or_dash(row.status),
        _or_dash(row.entries),
        newest,
        _or_dash(row.links),
        " ".join(row.error.split()) if row.error else "-",
    ]


def _or_dash(value: int | None) -> str:
    return "-" if value is None else str(value)


def _check(source: Source, world: World) -> SourceCheck:
    row = SourceCheck(name=source.name, type=source.type)
    try:
        # No ETag or Last-Modified: we want the full current content.
        response = world.fetch(source.url)
    except FetchError as e:
        return replace(row, error=str(e))
    row = replace(row, status=response.status)
    if response.status >= 400:
        return replace(row, error=f"HTTP {response.status}")
    if isinstance(source, HtmlIndexSource):
        return _check_index(row, source, response.body)
    return _check_feed(row, source, response.body)


def _check_feed(row: SourceCheck, source: Source, body: bytes) -> SourceCheck:
    try:
        entries = parse_feed(body)
    except NotAFeed as e:
        return replace(row, error=str(e))
    if source.type == "youtube":
        # Safety net: the long-form playlist should already leave Shorts out.
        entries = [e for e in entries if "/shorts/" not in e.get("link", "")]
    dates = [d for d in map(entry_date, entries) if d is not None]
    return replace(row, entries=len(entries), newest=max(dates).date() if dates else None)


def _check_index(row: SourceCheck, source: HtmlIndexSource, body: bytes) -> SourceCheck:
    links = article_links(source, body)
    if not links:
        return replace(row, links=0, error="no article link matches link_regex")
    return replace(row, links=len(links))


def article_links(source: HtmlIndexSource, body: bytes) -> list[str]:
    """Unique article URLs on an index page: same host, path matches, not excluded."""
    soup = BeautifulSoup(body, "html.parser")
    host = _host(source.url)
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(source.url, str(a["href"]))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or _host(url) != host:
            continue
        path = parts.path
        if not re.search(source.link_regex, path):
            continue
        if any(re.search(pattern, path) for pattern in source.exclude):
            continue
        found.setdefault(path.rstrip("/"), f"{parts.scheme}://{parts.netloc}{path}")
    return list(found.values())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")
