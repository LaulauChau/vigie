from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vigie.config import Config
from vigie.world import FetchError, Response, TranscriptSegment

FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeWorld:
    """Serves canned responses by URL and records every fetch.

    Transcripts are scripted by video id; a video with none scripted has no transcript.
    """

    def __init__(self, routes: dict[str, Response | Exception], now: datetime = NOW):
        self.routes = routes
        self.fetches: list[tuple[str, str | None, str | None]] = []
        # Tests move the clock forward to simulate a later run.
        self.current_time = now
        self.transcripts: dict[str, list[TranscriptSegment] | Exception] = {}
        self.transcript_requests: list[str] = []

    def fetch(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> Response:
        self.fetches.append((url, etag, last_modified))
        route = self.routes.get(url)
        if route is None:
            raise FetchError(f"no route for {url}")
        if isinstance(route, Exception):
            raise route
        return route

    def transcript(self, video_id: str) -> list[TranscriptSegment] | None:
        self.transcript_requests.append(video_id)
        scripted = self.transcripts.get(video_id)
        if isinstance(scripted, Exception):
            raise scripted
        return scripted

    def now(self) -> datetime:
        return self.current_time


def ok(name: str) -> Response:
    return Response(status=200, body=fixture(name))


def make_config(sources: list[dict[str, Any]]) -> Config:
    return Config.model_validate(
        {
            "settings": {
                "window_days": 7,
                "min_score": 6,
                "max_items": 5,
                "max_per_topic": 3,
                "shortlist_size": 10,
                "max_age_days": 30,
            },
            "topics": [{"id": "pratique-dev-data", "description": "Pratique."}],
            "sources": sources,
        }
    )

