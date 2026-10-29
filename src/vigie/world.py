"""The outside world: every effect Vigie has beyond its own memory.

The application receives one `World` object. Tests pass a fake; the CLI passes
`RealWorld`. Later capabilities (Slack) are added here too.
"""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import ollama
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
# The shared server may queue our requests behind colleagues' chats.
LLM_TIMEOUT = 300.0

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


# One chat message: {"role": "system" | "user" | "assistant", "content": ...}.
Message = dict[str, str]


@dataclass(frozen=True)
class ChatReply:
    # The model's answer, unparsed.
    text: str
    # Time the server spent loading the model for this request.
    load_seconds: float = 0.0


class LLMError(Exception):
    """Ollama could not be reached, or refused the request."""


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
        """The model's answer; `schema` constrains it to JSON. Raises LLMError."""
        ...

    def loaded_context_size(self, model: str) -> int | None:
        """The context size the model is loaded with on the server, or None if not loaded.

        Raises LLMError.
        """
        ...


class RealWorld:
    def __init__(self, timeout: float = 20.0, ollama_host: str | None = None) -> None:
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=timeout,
        )
        self._youtube = YouTubeTranscriptApi()
        self._ollama = ollama.Client(host=ollama_host, timeout=LLM_TIMEOUT)
        self._ollama_host = ollama_host or os.environ.get("OLLAMA_HOST") or "localhost:11434"

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
        options: dict[str, Any] = {}
        if num_ctx is not None:
            options["num_ctx"] = num_ctx
        if temperature is not None:
            options["temperature"] = temperature
        try:
            r = self._ollama.chat(
                model=model, messages=messages, format=schema, think=think, options=options
            )
        except _OLLAMA_ERRORS as e:
            raise self._llm_error(e) from e
        return ChatReply(text=r.message.content or "", load_seconds=(r.load_duration or 0) / 1e9)

    def loaded_context_size(self, model: str) -> int | None:
        try:
            running = self._ollama.ps().models
        except _OLLAMA_ERRORS as e:
            raise self._llm_error(e) from e
        wanted = _tagged(model)
        for m in running:
            if wanted in (_tagged(m.model or ""), _tagged(m.name or "")):
                return m.context_length
        return None

    def _llm_error(self, e: Exception) -> LLMError:
        return LLMError(f"Ollama at {self._ollama_host}: {type(e).__name__}: {e}")


# The client turns connection failures into ConnectionError but lets timeouts through.
_OLLAMA_ERRORS = (ollama.ResponseError, ollama.RequestError, ConnectionError, httpx.HTTPError)


def _tagged(model: str) -> str:
    """Ollama lists `qwen3.8` as `qwen3.8:latest`."""
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
