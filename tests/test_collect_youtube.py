from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeWorld, make_config, ok

from vigie.collect import collect
from vigie.config import YOUTUBE_PLAYLIST_FEED
from vigie.store import Store, open_store
from vigie.world import FetchError, Response, TranscriptSegment

CHANNEL = {"name": "Example Channel", "type": "youtube", "playlist_id": "UULFexample"}
FEED = YOUTUBE_PLAYLIST_FEED.format("UULFexample")
WORKFLOW = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
SHORT = "https://www.youtube.com/shorts/bbbbbbbbbbb"
PARALLEL = "https://www.youtube.com/watch?v=ccccccccccc"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "vigie.db")


def talk(segments: int, last_end: tuple[float, float]) -> list[TranscriptSegment]:
    """A transcript of six-word segments, the last one at (start, duration)."""
    lines = [
        TranscriptSegment(f"this is line {i}\nof talk", float(i), 1.0) for i in range(segments)
    ]
    lines[-1] = TranscriptSegment(lines[-1].text, *last_end)
    return lines


def channel_world() -> FakeWorld:
    world = FakeWorld({FEED: ok("youtube-playlist.xml")})
    world.transcripts = {
        "aaaaaaaaaaa": talk(30, (1796.0, 3.4)),
        "ccccccccccc": talk(25, (900.0, 2.0)),
    }
    return world


def test_youtube_videos_are_stored_with_their_transcript_and_duration(store: Store) -> None:
    world = channel_world()

    report = collect(make_config([CHANNEL]), store, world)

    assert report.new == 2
    assert report.errors == {}
    items = {item.url: item for item in store.items()}
    assert set(items) == {WORKFLOW, PARALLEL}
    video = items[WORKFLOW]
    assert video.source == "Example Channel"
    assert video.title == "My Claude Code workflow"
    assert video.kind == "video"
    assert video.word_count == 180
    assert video.content is not None
    assert video.content.startswith("this is line 0 of talk this is line 1 of talk")
    # The end of the last segment: 1796.0 + 3.4 seconds.
    assert video.duration_s == 1799
    assert items[PARALLEL].duration_s == 902
    assert items[PARALLEL].word_count == 150


def test_a_short_is_ignored(store: Store) -> None:
    world = channel_world()

    collect(make_config([CHANNEL]), store, world)

    assert SHORT not in {item.url for item in store.items()}
    assert "bbbbbbbbbbb" not in world.transcript_requests


def test_video_without_a_transcript_is_skipped_and_never_retried(store: Store) -> None:
    world = channel_world()
    del world.transcripts["ccccccccccc"]
    config = make_config([CHANNEL])

    report = collect(config, store, world)

    assert (report.new, report.skipped) == (1, 1)
    [video] = [item for item in store.items() if item.url == PARALLEL]
    assert video.kind == "video"
    assert video.content is None
    assert video.skip_reason == "no transcript"

    world.transcript_requests.clear()
    report = collect(config, store, world)

    assert (report.new, report.skipped) == (0, 0)
    assert world.transcript_requests == []


def test_second_run_adds_nothing(store: Store) -> None:
    world = channel_world()
    world.routes[FEED] = Response(
        status=200, body=ok("youtube-playlist.xml").body, headers={"etag": '"v1"'}
    )
    config = make_config([CHANNEL])
    collect(config, store, world)
    before = store.items()
    world.fetches.clear()
    world.transcript_requests.clear()

    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (0, 0, 0)
    assert store.items() == before
    assert world.fetches == [(FEED, '"v1"', None)]
    assert world.transcript_requests == []


def test_video_with_too_short_a_transcript_is_skipped(store: Store) -> None:
    world = channel_world()
    world.transcripts["ccccccccccc"] = [TranscriptSegment("thanks for watching", 0.0, 2.0)]

    report = collect(make_config([CHANNEL]), store, world)

    assert report.skipped == 1
    [video] = [item for item in store.items() if item.url == PARALLEL]
    assert video.skip_reason == "too little text (3 words)"


def test_transcript_that_cannot_be_fetched_is_retried_next_run(store: Store) -> None:
    world = channel_world()
    world.transcripts["ccccccccccc"] = FetchError("RequestBlocked: too many requests")
    config = make_config([CHANNEL])

    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (1, 0, 1)
    [video] = [item for item in store.items() if item.url == PARALLEL]
    assert video.skip_reason is None

    world.transcripts["ccccccccccc"] = talk(25, (900.0, 2.0))
    report = collect(config, store, world)

    assert (report.new, report.skipped, report.unreachable) == (1, 0, 0)
    [video] = [item for item in store.items() if item.url == PARALLEL]
    assert video.word_count == 150


def test_videos_older_than_max_age_days_are_not_stored(store: Store) -> None:
    # 30 days before this, 2026-10-22 12:00, falls between the two long-form videos.
    world = channel_world()
    world.current_time = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    collect(make_config([CHANNEL]), store, world)

    assert {item.url for item in store.items()} == {WORKFLOW}


def test_a_failing_youtube_source_is_recorded_and_the_others_are_collected(store: Store) -> None:
    broken = {"name": "Broken Channel", "type": "youtube", "playlist_id": "UULFbroken"}
    world = channel_world()
    world.routes[YOUTUBE_PLAYLIST_FEED.format("UULFbroken")] = Response(status=404, body=b"")

    report = collect(make_config([broken, CHANNEL]), store, world)

    assert report.errors == {"Broken Channel": "HTTP 404"}
    assert report.new == 2
    errors = {source.name: source.last_error for source in store.sources()}
    assert errors == {"Broken Channel": "HTTP 404", "Example Channel": None}
