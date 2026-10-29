"""The outside world: every effect Vigie has beyond its own memory.

The application receives one `World` object. Tests pass a fake; the CLI passes
`RealWorld`. Later capabilities (transcripts, LLM, Slack) are added here too.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import httpx

USER_AGENT = "vigie/0.1 (+https://github.com/LaulauChau/vigie)"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    # Header names are lowercase.
    headers: dict[str, str] = field(default_factory=dict)


class FetchError(Exception):
    """The request never got an HTTP response (DNS, connection, timeout...)."""


class World(Protocol):
    def fetch(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> Response: ...

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
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e
        return Response(
            status=r.status_code,
            body=r.content,
            headers={k.lower(): v for k, v in r.headers.items()},
        )

    def now(self) -> datetime:
        return datetime.now(UTC)
