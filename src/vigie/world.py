"""The outside world: every effect Vigie has beyond its own memory.

The application receives one `World` object. Tests pass a fake; the CLI passes
`RealWorld`. Later capabilities (LLM, Slack) are added here too.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx
import requests
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)

USER_AGENT = "vigie/0.1 (+https://github.com/LaulauChau/vigie)"

# Transcript failures that say nothing about the video itself: YouTube is down,
# is blocking us, or changed in a way the library cannot handle yet.
TRANSIENT_TRANSCRIPT_ERRORS = (
    RequestBlocked,
    YouTubeRequestFailed,
    PoTokenRequired,
    YouTubeDataUnparsable,
    FailedToCreateConsentCookie,
    NoTranscriptFound,
    requests.RequestException,
)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    # Header names are lowercase.
    headers: dict[str, str] = field(default_factory=dict)


class FetchError(Exception):
    """The request never got an HTTP response (DNS, connection, timeout...)."""


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    # Seconds from the start of the video.
    start: float
    duration: float


class World(Protocol):
    def fetch(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> Response: ...

    def transcript(self, video_id: str) -> list[TranscriptSegment] | None:
        """A YouTube video's transcript, or None if it has none.

        Raises FetchError when YouTube cannot be reached or refuses the request,
        so the video can be tried again on the next run.
        """
        ...

    def now(self) -> datetime:
        """The current time, timezone-aware."""
        ...


class RealWorld:
    def __init__(self, timeout: float = 20.0) -> None:
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=timeout,
        )
        self._youtube = YouTubeTranscriptApi()

    def fetch(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> Response:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            r = self._http.get(url, headers=headers)
        # InvalidURL (a mistyped Source URL) is not an HTTPError subclass.
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e
        return Response(
            status=r.status_code,
            body=r.content,
            headers={k.lower(): v for k, v in r.headers.items()},
        )

    def transcript(self, video_id: str) -> list[TranscriptSegment] | None:
        try:
            transcripts = self._youtube.list(video_id)
            try:
                chosen = transcripts.find_transcript(["en"])
            except NoTranscriptFound:
                # No English transcript: any language beats none.
                fallback = next(iter(transcripts), None)
                if fallback is None:
                    return None
                chosen = fallback
            fetched = chosen.fetch()
        except TRANSIENT_TRANSCRIPT_ERRORS as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e
        # Disabled subtitles, unavailable video and the like: there is no transcript.
        except CouldNotRetrieveTranscript:
            return None
        return [TranscriptSegment(s.text, s.start, s.duration) for s in fetched]

    def now(self) -> datetime:
        return datetime.now(UTC)
