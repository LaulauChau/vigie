from datetime import date
from typing import Any

from fakes import FakeWorld, make_config, ok

from vigie.check_sources import check_sources, render_table
from vigie.world import FetchError, Response

BLOG = {"name": "Example blog", "type": "rss", "url": "https://blog.example.com/feed"}


def test_rss_source_reports_status_entries_and_newest_date() -> None:
    world = FakeWorld({BLOG["url"]: ok("blog.rss.xml")})

    [row] = check_sources(make_config([BLOG]), world)

    assert row.name == "Example blog"
    assert row.type == "rss"
    assert row.status == 200
    assert row.entries == 3
    assert row.newest == date(2026, 10, 27)
    assert row.error is None


GONE = {"name": "Gone blog", "type": "rss", "url": "https://gone.example.com/feed"}


def test_http_error_is_reported_and_other_sources_still_run() -> None:
    world = FakeWorld(
        {
            GONE["url"]: Response(status=404, body=b"<html>Not found</html>"),
            BLOG["url"]: ok("blog.rss.xml"),
        }
    )

    gone, blog = check_sources(make_config([GONE, BLOG]), world)

    assert gone.status == 404
    assert gone.error == "HTTP 404"
    assert gone.entries is None
    assert blog.entries == 3
    assert blog.error is None


def test_network_error_is_reported_and_other_sources_still_run() -> None:
    world = FakeWorld(
        {
            GONE["url"]: FetchError("ConnectError: [Errno 8] nodename nor servname provided"),
            BLOG["url"]: ok("blog.rss.xml"),
        }
    )

    gone, blog = check_sources(make_config([GONE, BLOG]), world)

    assert gone.status is None
    assert gone.error == "ConnectError: [Errno 8] nodename nor servname provided"
    assert blog.entries == 3


def test_body_that_is_not_a_feed_is_reported() -> None:
    world = FakeWorld({BLOG["url"]: Response(status=200, body=b"<html><p>Cookie wall</p></html>")})

    [row] = check_sources(make_config([BLOG]), world)

    assert row.status == 200
    assert row.entries is None
    assert row.error is not None
    assert row.error.startswith("not a feed")


def test_youtube_source_reads_long_form_playlist_feed_and_ignores_shorts() -> None:
    playlist_feed = "https://www.youtube.com/feeds/videos.xml?playlist_id=UULFexample"
    world = FakeWorld({playlist_feed: ok("youtube-playlist.xml")})
    source = {"name": "Example Channel", "type": "youtube", "playlist_id": "UULFexample"}

    [row] = check_sources(make_config([source]), world)

    assert world.fetches == [(playlist_feed, None, None)]
    assert row.type == "youtube"
    assert row.entries == 2
    assert row.newest == date(2026, 10, 24)


ENGINEERING: dict[str, Any] = {
    "name": "Anthropic Engineering",
    "type": "html_index",
    "url": "https://www.anthropic.com/engineering",
    "link_regex": "^/research/[^/]+/?$",
    "exclude": ["^/research/team(/|$)"],
}


def test_html_index_counts_unique_article_links_after_exclusions() -> None:
    world = FakeWorld({ENGINEERING["url"]: ok("engineering-index.html")})

    [row] = check_sources(make_config([ENGINEERING]), world)

    assert row.type == "html_index"
    assert row.status == 200
    assert row.links == 3
    assert row.error is None


def test_html_index_with_no_matching_link_is_reported() -> None:
    world = FakeWorld(
        {ENGINEERING["url"]: Response(status=200, body=b"<html><a href='/news/x'>x</a></html>")}
    )

    [row] = check_sources(make_config([ENGINEERING]), world)

    assert row.links == 0
    assert row.error == "no article link matches link_regex"


def test_table_shows_one_row_per_source_with_age_of_newest_entry() -> None:
    world = FakeWorld(
        {
            BLOG["url"]: ok("blog.rss.xml"),
            GONE["url"]: Response(status=404, body=b""),
            ENGINEERING["url"]: ok("engineering-index.html"),
        }
    )

    table = render_table(check_sources(make_config([BLOG, GONE, ENGINEERING]), world))

    header, rule, blog, gone, engineering = table.splitlines()
    assert header.split() == ["Source", "Type", "HTTP", "Entries", "Newest", "Links", "Error"]
    assert set(rule) <= {"-", " "}
    assert blog.split() == ["Example", "blog", "rss", "200", "3", "2026-10-27", "(2d", "ago)", "-", "-"]
    assert gone.split() == ["Gone", "blog", "rss", "404", "-", "-", "-", "HTTP", "404"]
    assert engineering.split() == ["Anthropic", "Engineering", "html_index", "200", "-", "-", "3", "-"]
