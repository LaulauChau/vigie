from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import NOW, FakeWorld, fixture, make_config, ok

from vigie.collect import collect, render_summary
from vigie.store import Store, open_store
from vigie.world import FetchError, Response

BLOG = {"name": "Example blog", "type": "rss", "url": "https://blog.example.com/feed"}
PARALLEL_AGENTS = "https://blog.example.com/parallel-agents"
REVIEWING = "https://blog.example.com/reviewing-generated-code"
MCP = "https://blog.example.com/mcp-data-team"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "vigie.db")


def test_rss_items_are_stored_as_articles_with_their_page_text(store: Store) -> None:
    world = FakeWorld(
        {
            BLOG["url"]: ok("blog.rss.xml"),
            PARALLEL_AGENTS: ok("article.html"),
            REVIEWING: ok("article.html"),
            MCP: ok("article.html"),
        }
    )

    report = collect(make_config([BLOG]), store, world)

    assert report.new == 3
    assert report.skipped == 0
    assert report.errors == {}
    items = {item.url: item for item in store.items()}
    assert set(items) == {PARALLEL_AGENTS, REVIEWING, MCP}
    item = items[PARALLEL_AGENTS]
    assert item.source == "Example blog"
    assert item.title == "Running agents in parallel with worktrees"
    assert item.published_at == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert item.kind == "article"
    assert item.word_count == 205
    assert item.duration_s is None
    assert item.content is not None
    assert "For the last three months" in item.content
    assert item.skip_reason is None


def blog_world(pages: dict[str, str] | None = None) -> FakeWorld:
    """The blog feed with an ETag, and every article page long enough unless overridden."""
    feed = Response(
        status=200,
        body=fixture("blog.rss.xml"),
        headers={"etag": '"v1"', "last-modified": "Tue, 27 Oct 2026 09:30:00 GMT"},
    )
    routes: dict[str, Response | Exception] = {
        BLOG["url"]: feed,
        PARALLEL_AGENTS: ok("article.html"),
        REVIEWING: ok("article.html"),
        MCP: ok("article.html"),
    }
    routes.update({url: ok(name) for url, name in (pages or {}).items()})
    return FakeWorld(routes)


def test_second_run_is_conditional_and_adds_nothing(store: Store) -> None:
    world = blog_world()
    config = make_config([BLOG])
    collect(config, store, world)
    before = store.items()
    world.fetches.clear()

    report = collect(config, store, world)

    assert report.new == 0
    assert report.skipped == 0
    assert store.items() == before
    # Only the feed is fetched again, with the validators of the first response.
    assert world.fetches == [(BLOG["url"], '"v1"', "Tue, 27 Oct 2026 09:30:00 GMT")]


def test_not_modified_feed_adds_nothing_and_is_not_an_error(store: Store) -> None:
    world = blog_world()
    config = make_config([BLOG])
    collect(config, store, world)
    world.routes[BLOG["url"]] = Response(status=304, body=b"")

    report = collect(config, store, world)

    assert report.new == 0
    assert report.errors == {}
    assert len(store.items()) == 3
    [source] = store.sources()
    assert source.last_error is None
    # The validators are kept for the next run.
    assert source.etag == '"v1"'
    assert source.last_modified == "Tue, 27 Oct 2026 09:30:00 GMT"


HN = {
    "name": 'HN "coding agent"',
    "type": "rss",
    "url": "https://hnrss.org/newest?q=%22coding+agent%22&points=100&count=50",
}
HN_COPY = "https://www.Blog.Example.com/parallel-agents/?utm_source=hnrss&utm_medium=rss#comments"
OTHER = "https://other.example.org/coding-agents-at-work"


def test_same_article_from_two_sources_is_stored_once_under_the_first(store: Store) -> None:
    world = blog_world()
    world.routes.update(
        {HN["url"]: ok("hn.rss.xml"), HN_COPY: ok("article.html"), OTHER: ok("article.html")}
    )

    report = collect(make_config([BLOG, HN]), store, world)

    assert report.new == 4
    by_url = {item.url: item.source for item in store.items()}
    assert by_url == {
        PARALLEL_AGENTS: "Example blog",
        REVIEWING: "Example blog",
        MCP: "Example blog",
        # HN Items point to the external article, not the HN thread.
        OTHER: 'HN "coding agent"',
    }
    fetched = [url for url, _, _ in world.fetches]
    assert HN_COPY not in fetched


SPACE = {"name": "Example Space", "type": "rss", "url": "https://space.example.com/feed"}
EPISODE = "https://space.example.com/p/episode-12"
ESSAY = "https://space.example.com/p/cost-of-tokens"
OLD_EPISODE = "https://space.example.com/p/episode-1"


def space_world() -> FakeWorld:
    return FakeWorld(
        {
            SPACE["url"]: ok("podcast.rss.xml"),
            EPISODE: ok("article.html"),
            ESSAY: ok("article.html"),
            OLD_EPISODE: ok("article.html"),
        }
    )


def test_items_older_than_max_age_days_are_not_stored(store: Store) -> None:
    # NOW is 2026-10-29 and max_age_days is 30: the 2026-08-01 episode is too old.
    collect(make_config([SPACE]), store, space_world())

    assert OLD_EPISODE not in {item.url for item in store.items()}


def test_audio_enclosure_makes_a_podcast_with_its_itunes_duration(store: Store) -> None:
    collect(make_config([SPACE]), store, space_world())

    items = {item.url: item for item in store.items()}
    episode = items[EPISODE]
    assert episode.kind == "podcast"
    assert episode.duration_s == 3723
    essay = items[ESSAY]
    assert essay.kind == "article"
    assert essay.duration_s is None
    assert essay.word_count == 205


def test_item_without_a_date_is_dated_when_first_seen(store: Store) -> None:
    collect(make_config([SPACE]), store, space_world())

    [essay] = [item for item in store.items() if item.url == ESSAY]
    assert essay.published_at == NOW
    assert essay.first_seen_at == NOW


def test_item_with_too_little_text_is_skipped_and_never_extracted_again(store: Store) -> None:
    world = blog_world({REVIEWING: "teaser.html"})
    config = make_config([BLOG])

    report = collect(config, store, world)

    assert report.new == 2
    assert report.skipped == 1
    [teaser] = [item for item in store.items() if item.url == REVIEWING]
    assert teaser.content is None
    assert teaser.skip_reason == "too little text (17 words)"

    world.fetches.clear()
    report = collect(config, store, world)

    assert report.skipped == 0
    assert REVIEWING not in [url for url, _, _ in world.fetches]


def test_page_with_no_text_is_skipped(store: Store) -> None:
    world = blog_world()
    world.routes[MCP] = Response(status=200, body=b"<html><body></body></html>")

    report = collect(make_config([BLOG]), store, world)

    assert report.skipped == 1
    [empty] = [item for item in store.items() if item.url == MCP]
    assert empty.skip_reason == "no text extracted"


DOWN = {"name": "Down blog", "type": "rss", "url": "https://down.example.com/feed"}
BROKEN = {"name": "Broken blog", "type": "rss", "url": "https://broken.example.com/feed"}
WALLED = {"name": "Walled blog", "type": "rss", "url": "https://walled.example.com/feed"}


def test_failing_sources_record_their_error_and_the_others_are_collected(store: Store) -> None:
    world = blog_world()
    world.routes.update(
        {
            DOWN["url"]: FetchError("ConnectError: connection refused"),
            BROKEN["url"]: Response(status=500, body=b"oops"),
            WALLED["url"]: Response(status=200, body=b"<html><p>Cookie wall</p></html>"),
        }
    )

    report = collect(make_config([DOWN, BROKEN, WALLED, BLOG]), store, world)

    assert report.new == 3
    assert set(report.errors) == {"Down blog", "Broken blog", "Walled blog"}
    assert report.errors["Down blog"] == "ConnectError: connection refused"
    assert report.errors["Broken blog"] == "HTTP 500"
    assert report.errors["Walled blog"].startswith("not a feed")
    errors = {source.name: source.last_error for source in store.sources()}
    assert errors == {**report.errors, "Example blog": None}


def test_a_source_error_is_cleared_once_it_works_again(store: Store) -> None:
    world = blog_world()
    config = make_config([BLOG])
    world.routes[BLOG["url"]] = Response(status=503, body=b"")
    collect(config, store, world)
    world.routes[BLOG["url"]] = ok("blog.rss.xml")

    report = collect(config, store, world)

    assert report.errors == {}
    [source] = store.sources()
    assert source.last_error is None
    assert len(store.items()) == 3


def test_unreachable_page_is_retried_next_run_and_a_missing_one_is_skipped(
    store: Store,
) -> None:
    world = blog_world()
    world.routes[PARALLEL_AGENTS] = FetchError("ReadTimeout: timed out")
    world.routes[MCP] = Response(status=404, body=b"<html>Not found</html>")
    config = make_config([BLOG])

    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (1, 1, 1)
    items = {item.url: item for item in store.items()}
    assert items[MCP].skip_reason == "HTTP 404"
    assert items[PARALLEL_AGENTS].skip_reason is None

    world.routes[PARALLEL_AGENTS] = ok("article.html")
    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (1, 0, 0)
    [retried] = [item for item in store.items() if item.url == PARALLEL_AGENTS]
    assert retried.word_count == 205


def test_summary_counts_new_and_skipped_items_and_lists_sources_in_error(store: Store) -> None:
    world = blog_world({REVIEWING: "teaser.html"})
    world.routes[MCP] = FetchError("ReadTimeout: timed out")
    world.routes[DOWN["url"]] = FetchError("ConnectError: connection refused")

    summary = render_summary(collect(make_config([DOWN, BLOG]), store, world))

    assert summary.splitlines() == [
        "New Items: 1",
        "Skipped Items: 1",
        "Item pages unreachable, retried next run: 1",
        "Sources in error: 1",
        "  - Down blog: ConnectError: connection refused",
    ]


def test_summary_of_a_quiet_run(store: Store) -> None:
    world = blog_world()
    world.routes[BLOG["url"]] = Response(status=304, body=b"")

    summary = render_summary(collect(make_config([BLOG]), store, world))

    assert summary.splitlines() == ["New Items: 0", "Skipped Items: 0", "Sources in error: 0"]


def test_page_unreachable_until_past_max_age_days_is_skipped(store: Store) -> None:
    world = blog_world()
    world.routes[PARALLEL_AGENTS] = FetchError("ReadTimeout: timed out")
    config = make_config([BLOG])
    collect(config, store, world)
    # Published 2026-10-20: past the 30-day limit a month later.
    world.current_time = NOW + timedelta(days=30)

    report = collect(config, store, world)

    assert (report.skipped, report.unreachable) == (1, 0)
    [item] = [item for item in store.items() if item.url == PARALLEL_AGENTS]
    assert item.skip_reason == "page unreachable: ReadTimeout: timed out"


def test_store_is_created_on_first_run_with_its_folder(tmp_path: Path) -> None:
    store = open_store(tmp_path / "data" / "vigie.db")

    report = collect(make_config([BLOG]), store, blog_world())

    assert report.new == 3
