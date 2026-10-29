"""`vigie digest`: this week's Digest, built from the assessed Items and written as Markdown."""

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from vigie.config import Config, Settings
from vigie.store import Assessment, Digest, Item, Store
from vigie.world import World

INTRO = "Ce qu'il fallait retenir cette semaine sur l'IA dans le dev et la data."
FOOTER = (
    "Réagissez avec 1️⃣–5️⃣ sur les contenus qui vous ont été utiles (👎 si aucun),"
    " et discutons-en dans le fil."
)
KIND_ICONS = {"article": "📄", "video": "🎥", "podcast": "🎙️"}
WORDS_PER_MINUTE = 230


@dataclass(frozen=True)
class ShortlistEntry:
    """An assessed Item in the Window, and what became of it in the Digest."""

    item: Item
    assessment: Assessment
    # Set once the Shortlist is walked: the Pick number, or why the Item was dropped.
    pick: int | None = None
    dropped: str | None = None


@dataclass
class DigestReport:
    # ISO week, such as 2026-W44.
    week: str
    # None when there was no Pick: nothing was stored or written.
    path: Path | None
    picks: int = 0
    # True when the Digest already stored for the week was written again, unchanged.
    reused: bool = False
    # Set when there was no Pick, to tell a quiet week from a broken run.
    collected: int = 0
    assessed: int = 0
    best_score: int | None = None
    min_score: int = 0


def build_digest(
    config: Config,
    store: Store,
    world: World,
    out_dir: Path,
    *,
    rebuild: bool = False,
    since: timedelta | None = None,
) -> DigestReport:
    """`vigie digest`: build this week's Digest, store it and write its Markdown file.

    When a Digest is already stored for the week, it is written again as is, unless
    `rebuild` or `since` asks for a new one. `since` replaces `window_days` as the Window.
    With no Pick, nothing is stored or written.
    """
    now = world.now()
    year, week_number, _ = now.isocalendar()
    week = f"{year}-W{week_number:02d}"
    stored = store.digest(week)
    if stored is not None and not rebuild and since is None:
        path = _write(out_dir, stored)
        return DigestReport(week=week, path=path, picks=len(stored.pick_ids), reused=True)
    start = now - (since or timedelta(days=config.settings.window_days))
    in_window = [i for i in store.items() if i.published_at >= start]
    assessments = {a.item_id: a for a in store.assessments()}
    assessed = [ShortlistEntry(i, assessments[i.id]) for i in in_window if i.id in assessments]
    shortlist = _choose(_shortlist(assessed, config.settings), config.settings)
    picks = _picks(shortlist)
    if not picks:
        return DigestReport(
            week=week,
            path=None,
            collected=len(in_window),
            assessed=len(assessed),
            best_score=max((c.assessment.score for c in assessed), default=None),
            min_score=config.settings.min_score,
        )
    digest = Digest(
        week=week,
        window_start=start,
        window_end=now,
        pick_ids=tuple(c.item.id for c in picks),
        rendered_md=_render_message(now.date(), picks) + _render_appendix(start, now, shortlist),
        built_at=now,
    )
    store.save_digest(digest)
    path = _write(out_dir, digest)
    return DigestReport(week=week, path=path, picks=len(picks))


def render_digest_summary(report: DigestReport) -> str:
    if report.path is None:
        best = "none" if report.best_score is None else str(report.best_score)
        return "\n".join(
            [
                f"No Pick for {report.week}: nothing stored or written.",
                f"Items collected in the Window: {report.collected}",
                f"Items assessed in the Window: {report.assessed}",
                f"Best Score: {best}"
                + (f" (min_score is {report.min_score})" if report.best_score is not None else ""),
            ]
        )
    if report.reused:
        return (
            f"Digest for {report.week} already built, with {report.picks} Picks: {report.path}\n"
            "Pass --rebuild to build it again."
        )
    return f"Digest for {report.week} built with {report.picks} Picks: {report.path}"


def _write(out_dir: Path, digest: Digest) -> Path:
    """Write the Digest's Markdown into `out_dir`, in a file named after its build date."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{digest.built_at.date().isoformat()}.md"
    path.write_text(digest.rendered_md, encoding="utf-8")
    return path


def _shortlist(assessed: list[ShortlistEntry], settings: Settings) -> list[ShortlistEntry]:
    """The best `shortlist_size` eligible Items, best Score first, ties to the most recent."""
    eligible = [c for c in assessed if c.assessment.score >= settings.min_score]
    eligible.sort(key=lambda c: (c.assessment.score, c.item.published_at), reverse=True)
    return eligible[: settings.shortlist_size]


def _choose(shortlist: list[ShortlistEntry], settings: Settings) -> list[ShortlistEntry]:
    """Walk the Shortlist in order: skip full Topics, stop at `max_items`. Never top up."""
    picks = 0
    per_topic: dict[str, int] = {}
    walked = []
    for entry in shortlist:
        topic = entry.assessment.topic
        if picks == settings.max_items:
            entry = replace(entry, dropped=f"over the cap ({settings.max_items} Picks)")
        elif per_topic.get(topic, 0) == settings.max_per_topic:
            entry = replace(entry, dropped=f"Topic quota ({settings.max_per_topic} Picks)")
        else:
            per_topic[topic] = per_topic.get(topic, 0) + 1
            picks += 1
            entry = replace(entry, pick=picks)
        walked.append(entry)
    return walked


def _picks(shortlist: list[ShortlistEntry]) -> list[ShortlistEntry]:
    return [entry for entry in shortlist if entry.pick is not None]


def _render_message(built_on: date, picks: list[ShortlistEntry]) -> str:
    """The message, as it will be posted."""
    year, week, _ = built_on.isocalendar()
    monday = date.fromisocalendar(year, week, 1)
    blocks = [f"# 🧭 Veille IA — semaine du {monday:%d/%m}", INTRO]
    for n, pick in enumerate(picks, start=1):
        blocks += _render_pick(n, pick)
    blocks.append(FOOTER)
    return "\n\n".join(blocks) + "\n"


def _render_pick(n: int, pick: ShortlistEntry) -> list[str]:
    item, assessment = pick.item, pick.assessment
    length = _length(item)
    details = f"{length} · " if length else ""
    return [
        f"**{n}. {item.title}**  {KIND_ICONS[item.kind]} {details}{_domain(item.url)}",
        assessment.summary_fr,
        f"👥 Pour qui : {' · '.join(assessment.audience)}",
        f"🔗 {item.url}",
    ]


def _render_appendix(start: datetime, end: datetime, shortlist: list[ShortlistEntry]) -> str:
    """For the operator only: every Shortlist Item and what became of it."""
    lines = [
        "",
        "---",
        "",
        "## Review appendix (not sent)",
        "",
        f"Window: {start:%Y-%m-%d %H:%M %Z} → {end:%Y-%m-%d %H:%M %Z}. Order: Score.",
        "",
    ]
    for n, entry in enumerate(shortlist, start=1):
        assessment = entry.assessment
        outcome = f"Pick {entry.pick}" if entry.pick else f"dropped: {entry.dropped}"
        lines.append(
            f"{n}. **{entry.item.title}**: Score {assessment.score} · {assessment.topic}"
            f" · {outcome}"
        )
        lines.append(f"   {assessment.reason}")
    return "\n".join(lines) + "\n"


def _length(item: Item) -> str | None:
    """Reading time for an article, duration for a video or podcast; None when unknown."""
    if item.kind == "article":
        if not item.word_count:
            return None
        return f"{math.ceil(item.word_count / WORDS_PER_MINUTE)} min"
    if not item.duration_s:
        return None
    hours, minutes = divmod(max(1, round(item.duration_s / 60)), 60)
    return f"{hours} h {minutes:02d}" if hours else f"{minutes} min"


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")
