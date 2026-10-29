from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vigie.config import Config
from vigie.world import ChatReply, FetchError, Message, Response, TranscriptSegment

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
        # The fake LLM: scripted replies, answered in order, and the requests received.
        # A callable reply answers from the request it receives.
        self.replies: list[str | ChatReply | Exception | Callable[[ChatRequest], str]] = []
        self.chats: list[ChatRequest] = []
        # What the server's running-models endpoint reports, or raises.
        self.loaded_ctx: int | None | Exception = None
        # The fake Slack webhook: every payload posted, and an error to raise instead.
        self.slack_posts: list[dict[str, Any]] = []
        self.slack_error: Exception | None = None

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

    def chat(
        self,
        model: str,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        think: bool = False,
        num_ctx: int | None = None,
        temperature: float | None = None,
    ) -> ChatReply:
        request = ChatRequest(model, messages, schema, think, num_ctx, temperature)
        self.chats.append(request)
        if not self.replies:
            raise AssertionError("unexpected LLM call: no reply scripted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return ChatReply(text=reply(request))
        return ChatReply(text=reply) if isinstance(reply, str) else reply

    def loaded_context_size(self, model: str) -> int | None:
        if isinstance(self.loaded_ctx, Exception):
            raise self.loaded_ctx
        return self.loaded_ctx

    def post_slack(self, payload: dict[str, Any]) -> None:
        if self.slack_error is not None:
            raise self.slack_error
        self.slack_posts.append(payload)


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[Message]
    schema: dict[str, Any] | None
    think: bool
    num_ctx: int | None
    temperature: float | None


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

