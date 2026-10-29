"""`vigie collect`: store new Items from the Sources, with their text."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import trafilatura

from vigie.config import Config, RssSource, YoutubeSource
from vigie.feeds import Entry, NotAFeed, entry_date, is_podcast, itunes_duration, parse_feed
from vigie.store import Item, Kind, NewItem, SourceState, Store
from vigie.world import FetchError, World

# Below this, the page is a teaser or a paywall, not something worth assessing.
MIN_WORDS = 100
# Page statuses worth retrying on the next run, besides 5xx.
RETRY_STATUSES = {408, 429}


@dataclass
class CollectReport:
    # Items that got their text this run, ready to be assessed.
    new: int = 0
    # Items stored with a skip reason this run.
    skipped: int = 0
    # Item pages that could not be fetched this time; they are retried next run.
    unreachable: int = 0
    # Source name -> error.
    errors: dict[str, str] = field(default_factory=dict)


def collect(config: Config, store: Store, world: World) -> CollectReport:
    report = CollectReport()
    oldest = world.now() - timedelta(days=config.settings.max_age_days)
    for source in config.sources:
        if isinstance(source, (RssSource, YoutubeSource)):
            state = store.source(source.name, source.type, source.url)
            try:
                _collect_feed(source, state, oldest, store, world)
            # Whatever goes wrong with one Source, the others are still collected.
            except Exception as e:
                known = isinstance(e, (FetchError, NotAFeed))
                error = str(e) if known else f"{type(e).__name__}: {e}"
                store.record_error(state.id, world.now(), error)
                report.errors[source.name] = error
    for item in store.items_to_extract():
        _extract(item, oldest, store, world, report)
    return report


def render_summary(report: CollectReport) -> str:
    lines = [f"New Items: {report.new}", f"Skipped Items: {report.skipped}"]
    if report.unreachable:
        lines.append(f"Item pages unreachable, retried next run: {report.unreachable}")
    lines.append(f"Sources in error: {len(report.errors)}")
    lines += [f"  - {name}: {' '.join(error.split())}" for name, error in report.errors.items()]
    return "\n".join(lines)


def _collect_feed(
    source: RssSource | YoutubeSource,
    state: SourceState,
    oldest: datetime,
    store: Store,
    world: World,
) -> None:
    response = world.fetch(source.url, state.etag, state.last_modified)
    if response.status == 304:
        store.record_fetch(state.id, world.now(), state.etag, state.last_modified)
        return
    if response.status >= 400:
        raise FetchError(f"HTTP {response.status}")
    for entry in parse_feed(response.body):
        _add_entry(entry, source, state, oldest, store, world)
    store.record_fetch(
        state.id, world.now(), response.headers.get("etag"), response.headers.get("last-modified")
    )


def _add_entry(
    entry: Entry,
    source: RssSource | YoutubeSource,
    state: SourceState,
    oldest: datetime,
    store: Store,
    world: World,
) -> None:
    url = entry.get("link")
    if not url:
        return
    video = isinstance(source, YoutubeSource)
    # Safety net: the long-form playlist should already leave Shorts out.
    if video and "/shorts/" in url:
        return
    now = world.now()
    published_at = entry_date(entry) or now
    if published_at < oldest:
        return
    kind: Kind = "video" if video else "podcast" if is_podcast(entry) else "article"
    store.add_item(
        NewItem(
            source_id=state.id,
            url=url,
            title=entry.get("title", url),
            published_at=published_at,
            first_seen_at=now,
            kind=kind,
            # A video's duration comes with its transcript.
            duration_s=itunes_duration(entry) if kind == "podcast" else None,
        )
    )


def _extract(
    item: Item, oldest: datetime, store: Store, world: World, report: CollectReport
) -> None:
    """Store the Item's page text, or a skip reason, or leave it for the next run."""
    if item.kind == "video":
        _extract_transcript(item, oldest, store, world, report)
        return
    try:
        response = world.fetch(item.url)
    except FetchError as e:
        _unreachable(item, str(e), oldest, store, report)
        return
    if response.status in RETRY_STATUSES or response.status >= 500:
        _unreachable(item, f"HTTP {response.status}", oldest, store, report)
        return
    if response.status >= 400:
        reason = f"HTTP {response.status}"
    else:
        text = trafilatura.extract(response.body, url=item.url) or ""
        _keep_text(item, text, store, report)
        return
    store.skip(item.id, reason)
    report.skipped += 1


def _extract_transcript(
    item: Item, oldest: datetime, store: Store, world: World, report: CollectReport
) -> None:
    """Store the video's transcript, or a skip reason, or leave it for the next run."""
    video_id = parse_qs(urlsplit(item.url).query).get("v", [""])[0]
    if not video_id:
        store.skip(item.id, "no YouTube video id in the URL")
        report.skipped += 1
        return
    try:
        segments = world.transcript(video_id)
    except FetchError as e:
        _unreachable(item, str(e), oldest, store, report)
        return
    if not segments:
        store.skip(item.id, "no transcript")
        report.skipped += 1
        return
    text = " ".join(" ".join(s.text.split()) for s in segments)
    last = segments[-1]
    _keep_text(item, text, store, report, duration_s=round(last.start + last.duration))


def _keep_text(
    item: Item, text: str, store: Store, report: CollectReport, duration_s: int | None = None
) -> None:
    """Store the text if it is long enough to be worth assessing, else skip the Item."""
    words = len(text.split())
    if words >= MIN_WORDS:
        store.set_text(item.id, text, words, duration_s)
        report.new += 1
        return
    store.skip(item.id, f"too little text ({words} words)" if words else "no text extracted")
    report.skipped += 1


def _unreachable(
    item: Item, failure: str, oldest: datetime, store: Store, report: CollectReport
) -> None:
    if item.published_at >= oldest:
        report.unreachable += 1
        return
    # Past the age limit: no longer worth waiting for.
    store.skip(item.id, f"page unreachable: {failure}")
    report.skipped += 1
