from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fakes import NOW, FakeWorld, fixture, make_config, ok

from vigie.collect import collect
from vigie.store import Item, Store, open_store
from vigie.world import FetchError, Response

ENGINEERING: dict[str, Any] = {
    "name": "Anthropic Engineering",
    "type": "html_index",
    "url": "https://www.anthropic.com/engineering",
    "link_regex": "^/research/[^/]+/?$",
    "exclude": ["^/research/team(/|$)"],
}
AGENTS = "https://www.anthropic.com/research/building-effective-agents"
BEST_PRACTICES = "https://www.anthropic.com/research/claude-code-best-practices/"
RETRIEVAL = "https://www.anthropic.com/research/contextual-retrieval"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "vigie.db")


def engineering_world() -> FakeWorld:
    return FakeWorld(
        {
            ENGINEERING["url"]: ok("engineering-index.html"),
            AGENTS: ok("engineering-article.html"),
            BEST_PRACTICES: ok("engineering-article.html"),
            RETRIEVAL: ok("engineering-article.html"),
        }
    )


def test_links_matching_the_regex_become_items_with_their_page_text(store: Store) -> None:
    world = engineering_world()

    report = collect(make_config([ENGINEERING]), store, world)

    assert (report.new, report.skipped, report.errors) == (3, 0, {})
    items = {item.url: item for item in store.items()}
    assert set(items) == {AGENTS, BEST_PRACTICES, RETRIEVAL}
    item = items[AGENTS]
    assert item.source == "Anthropic Engineering"
    assert item.title == "Redesigning source control"
    assert item.kind == "article"
    assert item.duration_s is None
    assert item.word_count == 202
    assert item.content is not None
    assert "For the last three months" in item.content
    # Excluded, non-matching and other-host links are never fetched.
    assert [url for url, _, _ in world.fetches] == [
        ENGINEERING["url"],
        AGENTS,
        BEST_PRACTICES,
        RETRIEVAL,
    ]


def test_date_is_article_published_time_before_json_ld(store: Store) -> None:
    # The page states 2026-10-13 in article:published_time and 2026-10-12 in JSON-LD.
    collect(make_config([ENGINEERING]), store, engineering_world())

    [item] = [item for item in store.items() if item.url == AGENTS]
    assert item.published_at == datetime(2026, 10, 13, tzinfo=UTC)


CLAUDE_BLOG: dict[str, Any] = {
    "name": "Claude Blog",
    "type": "html_index",
    "url": "https://claude.com/resources/articles",
    "link_regex": "^/resources/articles/[^/]+/?$",
}
SUBAGENTS = "https://claude.com/resources/articles/building-agents-fold-subagents"
DATA_TEAMS = "https://claude.com/resources/articles/claude-for-data-teams"
REPORTS = "https://claude.com/resources/articles/smart-reports-surveys"
SUMMER = "https://claude.com/resources/articles/summer-article"


def claude_blog_world() -> FakeWorld:
    return FakeWorld(
        {
            CLAUDE_BLOG["url"]: ok("claude-blog-index.html"),
            SUBAGENTS: ok("claude-article.html"),
            DATA_TEAMS: ok("claude-article-time.html"),
            REPORTS: ok("claude-article-undated.html"),
            SUMMER: ok("old-article.html"),
        }
    )


def claude_blog_items(store: Store) -> dict[str, Item]:
    collect(make_config([CLAUDE_BLOG]), store, claude_blog_world())
    return {item.url: item for item in store.items()}


def test_date_is_json_ld_date_published_not_the_embedded_json_date(store: Store) -> None:
    # The page also embeds a related article's "date": 2026-10-27.
    item = claude_blog_items(store)[SUBAGENTS]

    assert item.published_at == datetime(2026, 10, 22, tzinfo=UTC)


def test_date_falls_back_to_the_date_trafilatura_finds(store: Store) -> None:
    # Only a <time> element on the page, besides the embedded related "date".
    item = claude_blog_items(store)[DATA_TEAMS]

    assert item.published_at == datetime(2026, 10, 15, tzinfo=UTC)


def test_page_with_no_date_is_dated_when_first_seen(store: Store) -> None:
    # The embedded related "date" is the only date on the page; it is not used.
    item = claude_blog_items(store)[REPORTS]

    assert item.published_at == NOW
    assert item.first_seen_at == NOW
    assert item.content is not None


def test_item_older_than_max_age_days_is_stored_skipped_and_never_fetched_again(
    store: Store,
) -> None:
    world = claude_blog_world()
    config = make_config([CLAUDE_BLOG])

    report = collect(config, store, world)

    # NOW is 2026-10-29 and max_age_days is 30: the 2026-08-01 article is too old.
    [old] = [item for item in store.items() if item.url == SUMMER]
    assert old.published_at == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    assert old.content is None
    assert old.skip_reason == "too old (published 2026-08-01)"
    # Ignored like a too-old feed entry: not counted as a skipped Item.
    assert (report.new, report.skipped) == (3, 0)

    world.fetches.clear()
    collect(config, store, world)

    assert SUMMER not in [url for url, _, _ in world.fetches]


def test_second_run_fetches_only_new_links_and_adds_nothing_for_stored_ones(
    store: Store,
) -> None:
    world = engineering_world()
    config = make_config([ENGINEERING])
    collect(config, store, world)
    before = store.items()
    world.fetches.clear()

    report = collect(config, store, world)

    assert (report.new, report.skipped) == (0, 0)
    assert store.items() == before
    assert [url for url, _, _ in world.fetches] == [ENGINEERING["url"]]

    new_article = "https://www.anthropic.com/research/writing-tools-for-agents"
    index = fixture("engineering-index.html").replace(
        b"<main>", b'<main><a href="/research/writing-tools-for-agents">Writing tools</a>'
    )
    world.routes[ENGINEERING["url"]] = Response(status=200, body=index)
    world.routes[new_article] = ok("engineering-article.html")
    world.fetches.clear()

    report = collect(config, store, world)

    assert report.new == 1
    assert [url for url, _, _ in world.fetches] == [ENGINEERING["url"], new_article]


def test_link_already_stored_from_another_source_is_not_fetched(store: Store) -> None:
    feed = {"name": "Feed", "type": "rss", "url": "https://feed.example.com/rss"}
    feed_link = "https://anthropic.com/research/building-effective-agents/?utm_source=rss"
    rss = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>'
        f"<item><title>Agents</title><link>{feed_link}</link>"
        "<pubDate>Tue, 27 Oct 2026 09:00:00 GMT</pubDate></item></channel></rss>"
    )
    world = engineering_world()
    world.routes[feed["url"]] = Response(status=200, body=rss.encode())
    world.routes[feed_link] = ok("article.html")

    collect(make_config([feed, ENGINEERING]), store, world)

    assert AGENTS not in [url for url, _, _ in world.fetches]
    assert {item.url: item.source for item in store.items()} == {
        feed_link: "Feed",
        BEST_PRACTICES: "Anthropic Engineering",
        RETRIEVAL: "Anthropic Engineering",
    }


def test_missing_or_short_page_is_skipped_and_an_unreachable_one_retried_next_run(
    store: Store,
) -> None:
    world = engineering_world()
    world.routes[AGENTS] = Response(status=404, body=b"<html>Not found</html>")
    world.routes[BEST_PRACTICES] = ok("teaser.html")
    world.routes[RETRIEVAL] = FetchError("ReadTimeout: timed out")
    config = make_config([ENGINEERING])

    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (0, 2, 1)
    items = {item.url: item for item in store.items()}
    assert items[AGENTS].skip_reason == "HTTP 404"
    assert items[AGENTS].published_at == NOW
    assert items[BEST_PRACTICES].skip_reason == "too little text (17 words)"
    # Not stored yet: its date is unknown until its page is read.
    assert RETRIEVAL not in items

    world.routes[RETRIEVAL] = Response(status=503, body=b"")
    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (0, 0, 1)

    world.routes[RETRIEVAL] = ok("engineering-article.html")
    world.fetches.clear()
    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (1, 0, 0)
    assert [url for url, _, _ in world.fetches] == [ENGINEERING["url"], RETRIEVAL]


def test_failing_index_records_its_error_and_the_other_sources_are_collected(
    store: Store,
) -> None:
    moved = {**CLAUDE_BLOG, "name": "Moved blog", "url": "https://moved.example.com/articles"}
    down = {**CLAUDE_BLOG, "name": "Down blog", "url": "https://down.example.com/articles"}
    world = engineering_world()
    world.routes[moved["url"]] = Response(status=200, body=b"<html><a href='/news/x'>x</a></html>")
    world.routes[down["url"]] = Response(status=500, body=b"oops")

    report = collect(make_config([moved, down, ENGINEERING]), store, world)

    assert report.new == 3
    assert report.errors == {
        "Moved blog": "no article link matches link_regex",
        "Down blog": "HTTP 500",
    }
    errors = {source.name: source.last_error for source in store.sources()}
    assert errors == {**report.errors, "Anthropic Engineering": None}
