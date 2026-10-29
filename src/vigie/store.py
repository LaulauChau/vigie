"""Store: the SQLite database holding Sources and Items."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from vigie.urls import url_hash

Kind = Literal["article", "video", "podcast"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_fetched_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    duration_s INTEGER,
    word_count INTEGER,
    content TEXT,
    skip_reason TEXT
);
"""


@dataclass(frozen=True)
class SourceState:
    id: int
    name: str
    etag: str | None
    last_modified: str | None
    last_fetched_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class NewItem:
    source_id: int
    url: str
    title: str
    published_at: datetime
    first_seen_at: datetime
    kind: Kind
    duration_s: int | None


@dataclass(frozen=True)
class Item:
    id: int
    source: str
    url: str
    title: str
    published_at: datetime
    first_seen_at: datetime
    kind: Kind
    duration_s: int | None
    word_count: int | None
    # Both empty until the text is extracted; then exactly one is set.
    content: str | None
    skip_reason: str | None


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    def source(self, name: str, type: str, url: str) -> SourceState:
        """The stored state of a Source, created on first sight. Type and URL follow the config."""
        with self._db:
            self._db.execute(
                "INSERT INTO sources (name, type, url) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET type = excluded.type, url = excluded.url",
                (name, type, url),
            )
        row = self._db.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
        return _source_state(row)

    def sources(self) -> list[SourceState]:
        return [_source_state(r) for r in self._db.execute("SELECT * FROM sources ORDER BY id")]

    def record_fetch(
        self, source_id: int, at: datetime, etag: str | None, last_modified: str | None
    ) -> None:
        """A successful fetch: keep the validators for the next conditional request."""
        with self._db:
            self._db.execute(
                "UPDATE sources SET etag = ?, last_modified = ?, last_fetched_at = ?,"
                " last_error = NULL WHERE id = ?",
                (etag, last_modified, at.isoformat(), source_id),
            )

    def record_error(self, source_id: int, at: datetime, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE sources SET last_fetched_at = ?, last_error = ? WHERE id = ?",
                (at.isoformat(), error, source_id),
            )

    def add_item(self, item: NewItem) -> None:
        """Store a new Item, unless one with the same normalized URL is already stored."""
        with self._db:
            self._db.execute(
                "INSERT INTO items (source_id, url, url_hash, title, published_at,"
                " first_seen_at, kind, duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (url_hash) DO NOTHING",
                (
                    item.source_id,
                    item.url,
                    url_hash(item.url),
                    item.title,
                    item.published_at.isoformat(),
                    item.first_seen_at.isoformat(),
                    item.kind,
                    item.duration_s,
                ),
            )

    def items(self) -> list[Item]:
        rows = self._db.execute(
            "SELECT items.*, sources.name AS source FROM items"
            " JOIN sources ON sources.id = items.source_id ORDER BY items.id"
        )
        return [_item(r) for r in rows]

    def items_to_extract(self) -> list[Item]:
        """Items with neither text nor skip reason yet."""
        return [i for i in self.items() if i.content is None and i.skip_reason is None]

    def set_text(
        self, item_id: int, content: str, word_count: int, duration_s: int | None = None
    ) -> None:
        """Store the Item's text; a duration, when given, replaces the stored one."""
        with self._db:
            self._db.execute(
                "UPDATE items SET content = ?, word_count = ?,"
                " duration_s = COALESCE(?, duration_s) WHERE id = ?",
                (content, word_count, duration_s, item_id),
            )

    def skip(self, item_id: int, reason: str) -> None:
        """The Item will never be extracted or assessed again."""
        with self._db:
            self._db.execute("UPDATE items SET skip_reason = ? WHERE id = ?", (reason, item_id))

    def close(self) -> None:
        self._db.close()


def open_store(path: Path) -> Store:
    """Open the database, creating it, its folder and its tables on first run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return Store(conn)


def _source_state(row: sqlite3.Row) -> SourceState:
    return SourceState(
        id=row["id"],
        name=row["name"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        last_fetched_at=_dt(row["last_fetched_at"]),
        last_error=row["last_error"],
    )


def _item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source=row["source"],
        url=row["url"],
        title=row["title"],
        published_at=datetime.fromisoformat(row["published_at"]),
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        kind=row["kind"],
        duration_s=row["duration_s"],
        word_count=row["word_count"],
        content=row["content"],
        skip_reason=row["skip_reason"],
    )


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
