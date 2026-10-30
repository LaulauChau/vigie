"""Reading HTML index Sources: article links on the index, metadata on the article page."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import trafilatura
from bs4 import BeautifulSoup
from trafilatura.settings import set_date_params

from vigie.config import HtmlIndexSource


class NoArticleLinks(Exception):
    """The index page has no link matching the Source's link regex: the site moved its articles."""


@dataclass(frozen=True)
class Article:
    title: str | None
    published_at: datetime | None


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


def read_article(body: bytes, now: datetime) -> Article:
    """The title and publication date of an article page, when the page states them."""
    soup = BeautifulSoup(body, "html.parser")
    published_at = _published_time(soup) or _json_ld_date(soup)
    # Claude Blog pages embed related articles' data, each with its own "date",
    # in scripts; trafilatura would take one of those for the page's date.
    for script in soup.find_all("script"):
        script.decompose()
    metadata = trafilatura.extract_metadata(
        str(soup), date_config={**set_date_params(), "max_date": now.date().isoformat()}
    )
    if published_at is None and metadata.date:
        published_at = _parse_date(metadata.date)
    title = metadata.title.strip() if metadata.title else None
    return Article(title=title or None, published_at=published_at)


def _published_time(soup: BeautifulSoup) -> datetime | None:
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    return _parse_date(meta.get("content")) if meta else None


def _json_ld_date(soup: BeautifulSoup) -> datetime | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        for node in _top_level_nodes(data):
            if parsed := _parse_date(node.get("datePublished")):
                return parsed
    return None


def _top_level_nodes(data: object) -> Iterator[dict[str, object]]:
    """The JSON-LD objects describing the page itself, including those in a `@graph`.

    Nested objects are left out: they describe other things, such as related articles.
    """
    nodes = data if isinstance(data, list) else [data]
    for node in nodes:
        if isinstance(node, dict):
            yield node
            graph = node.get("@graph")
            if isinstance(graph, list):
                yield from (n for n in graph if isinstance(n, dict))


def _parse_date(value: object) -> datetime | None:
    """An ISO 8601 date or datetime; one without a timezone is taken as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")
