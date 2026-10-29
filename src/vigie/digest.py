"""`vigie digest`: this week's Digest, built from the assessed Items and written as Markdown."""

import json
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from vigie.assess import CHARS_PER_TOKEN, DEFAULT_CONTEXT_TOKENS, warn_if_reloaded
from vigie.config import Config, Settings
from vigie.store import Assessment, Digest, Item, Store
from vigie.world import LLMError, Message, World

INTRO = "Ce qu'il fallait retenir cette semaine sur l'IA dans le dev et la data."
FOOTER = (
    "Réagissez avec 1️⃣–5️⃣ sur les contenus qui vous ont été utiles (👎 si aucun),"
    " et discutons-en dans le fil."
)
KIND_ICONS = {"article": "📄", "video": "🎥", "podcast": "🎙️"}
WORDS_PER_MINUTE = 230

# ADR 0001: the context size is reused, never chosen, so the Shortlist Items shown to the
# ranking call are cut to fit it. Kept free for the answer: thinking, then the JSON.
RANKING_ANSWER_TOKENS = 2048

# Written in English. The Shortlist Items are sent as a JSON array in the user message.
RANKING_SYSTEM = """\
You choose the picks of a weekly digest read by the staff of a French IT services company \
(ESN) focused on data and software: developers, data engineers and analysts, tech leads, \
managers, and the Cellule IA that hunts for AI use cases. They want to learn how \
practitioners actually use AI in day-to-day dev and data work.

The user message is this week's shortlist: the best-scored items, each with its id, title, \
domain, kind, topic, score (0 to 10), French summary and the reason for its score. Each item \
was scored on its own; now compare them against each other.

# Your answer

Answer with a JSON object only.
- keep: the ids of the items worth a reader's time, best first. Keep every such item, not \
just five: the number of picks and the mix of topics are handled afterwards.
- dropped: every other item, with its id and a reason:
  - "duplicate": it tells the same story as another item, such as one announcement covered \
by two blogs or one episode published twice. Keep the better of the two, and give its id as \
duplicate_of.
  - "weak": it is clearly weaker than the others, such as news, hype or promotion, or \
little a reader would learn. Its duplicate_of is null.
"""


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
    # Shortlist size: when above zero with no Pick, the ranking kept none of it.
    shortlisted: int = 0


def build_digest(
    config: Config,
    store: Store,
    world: World,
    model: str,
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
    ranked, order_label = _rank(_shortlist(assessed, config.settings), world, model)
    shortlist = _choose(ranked, config.settings)
    picks = _picks(shortlist)
    if not picks:
        return DigestReport(
            week=week,
            path=None,
            collected=len(in_window),
            assessed=len(assessed),
            best_score=max((c.assessment.score for c in assessed), default=None),
            min_score=config.settings.min_score,
            shortlisted=len(shortlist),
        )
    digest = Digest(
        week=week,
        window_start=start,
        window_end=now,
        pick_ids=tuple(c.item.id for c in picks),
        rendered_md=_render_message(now.date(), picks)
        + _render_appendix(start, now, order_label, shortlist),
        built_at=now,
    )
    store.save_digest(digest)
    path = _write(out_dir, digest)
    return DigestReport(week=week, path=path, picks=len(picks))


def render_digest_summary(report: DigestReport) -> str:
    if report.path is None:
        best = "none" if report.best_score is None else str(report.best_score)
        lines = [
            f"No Pick for {report.week}: nothing stored or written.",
            f"Items collected in the Window: {report.collected}",
            f"Items assessed in the Window: {report.assessed}",
            f"Best Score: {best}"
            + (f" (min_score is {report.min_score})" if report.best_score is not None else ""),
        ]
        if report.shortlisted:
            lines.append(f"The ranking kept none of the {report.shortlisted} Shortlist Items.")
        return "\n".join(lines)
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


# What the ranking call must answer. Its JSON schema is also passed to Ollama as `format`,
# so these models have no docstring: that would land in the schema.
class DroppedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    reason: Literal["duplicate", "weak"]
    duplicate_of: int | None


class RankingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep: list[int]
    dropped: list[DroppedItem]


def _rank(
    shortlist: list[ShortlistEntry], world: World, model: str
) -> tuple[list[ShortlistEntry], str]:
    """The Shortlist in the order to walk it, and that order's label for the appendix.

    The LLM compares the Items against each other. The ones it keeps come first, best
    first; the others follow in Score order, already dropped. When the call fails or its
    answer is unusable, the Shortlist stays in Score order.
    """
    if not shortlist:
        return shortlist, "LLM ranking"
    try:
        # Read and sent back as is (ADR 0001).
        num_ctx = world.loaded_context_size(model)
        shown = _fitting(shortlist, num_ctx or DEFAULT_CONTEXT_TOKENS)
        if not shown:
            return shortlist, _fallback("no Item fits the context size")
        reply = world.chat(
            model,
            _ranking_messages(shown),
            schema=RankingOutput.model_json_schema(),
            think=True,
            num_ctx=num_ctx,
        )
        warn_if_reloaded(reply, model, "the ranking")
        output = RankingOutput.model_validate_json(reply.text)
    except LLMError as e:
        return shortlist, _fallback(" ".join(str(e).split()))
    except ValidationError:
        return shortlist, _fallback("unusable output")
    # Items left out of the request count as unknown.
    by_id = {entry.item.id: entry for entry in shown}
    # Unknown ids are ignored, and so is any id after its first mention.
    kept_ids = [item_id for item_id in dict.fromkeys(output.keep) if item_id in by_id]
    if output.keep and not kept_ids:
        # Not one id it kept is in the Shortlist: it did not answer about these Items.
        return shortlist, _fallback("unusable output")
    reasons = {entry.item.id: "not ranked" for entry in shown}
    reasons |= {
        entry.item.id: "not ranked (left out to fit the context size)"
        for entry in shortlist[len(shown) :]
    }
    answered: set[int] = set()
    for drop in output.dropped:
        if drop.id not in by_id or drop.id in kept_ids or drop.id in answered:
            continue
        answered.add(drop.id)
        if drop.reason == "weak":
            reasons[drop.id] = "weak"
        elif drop.duplicate_of in by_id and drop.duplicate_of != drop.id:
            reasons[drop.id] = f'Duplicate of "{by_id[drop.duplicate_of].item.title}"'
        else:
            reasons[drop.id] = "Duplicate of an unknown Item"
    rest = [
        replace(entry, dropped=reasons[entry.item.id])
        for entry in shortlist
        if entry.item.id not in kept_ids
    ]
    return [by_id[item_id] for item_id in kept_ids] + rest, "LLM ranking"


def _fitting(shortlist: list[ShortlistEntry], context_tokens: int) -> list[ShortlistEntry]:
    """The best-scored Shortlist Items whose ranking request fits the context size."""
    budget = (context_tokens - RANKING_ANSWER_TOKENS) * CHARS_PER_TOKEN
    shown = list(shortlist)
    while shown and sum(len(m["content"]) for m in _ranking_messages(shown)) > budget:
        shown.pop()
    return shown


def _fallback(why: str) -> str:
    return f"Score, as the ranking failed ({why})"


def _ranking_messages(shortlist: list[ShortlistEntry]) -> list[Message]:
    items = [
        {
            "id": entry.item.id,
            "title": entry.item.title,
            "domain": _domain(entry.item.url),
            "kind": entry.item.kind,
            "topic": entry.assessment.topic,
            "score": entry.assessment.score,
            "summary": entry.assessment.summary_fr,
            "reason": entry.assessment.reason,
        }
        for entry in shortlist
    ]
    return [
        {"role": "system", "content": RANKING_SYSTEM},
        {"role": "user", "content": json.dumps(items, ensure_ascii=False, indent=1)},
    ]


def _choose(shortlist: list[ShortlistEntry], settings: Settings) -> list[ShortlistEntry]:
    """Walk the Shortlist in order: skip full Topics, stop at `max_items`. Never top up.

    Items already dropped by the ranking stay dropped.
    """
    picks = 0
    per_topic: dict[str, int] = {}
    walked = []
    for entry in shortlist:
        topic = entry.assessment.topic
        if entry.dropped is not None:
            walked.append(entry)
            continue
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


def _render_appendix(
    start: datetime, end: datetime, order_label: str, shortlist: list[ShortlistEntry]
) -> str:
    """For the operator only: every Shortlist Item and what became of it."""
    lines = [
        "",
        "---",
        "",
        "## Review appendix (not sent)",
        "",
        f"Window: {start:%Y-%m-%d %H:%M %Z} → {end:%Y-%m-%d %H:%M %Z}. Order: {order_label}.",
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
