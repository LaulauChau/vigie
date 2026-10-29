import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import NOW, ChatRequest, FakeWorld, make_config

from vigie.assess import score
from vigie.config import Config
from vigie.digest import DigestReport, build_digest, render_digest_summary
from vigie.store import Kind, NewItem, Store, open_store
from vigie.world import LLMError

MODEL = "qwen3.8"
# NOW is Thursday 29 October 2026, in ISO week 44, whose Monday is 26 October.
CONFIG = make_config([{"name": "Example blog", "type": "rss", "url": "https://blog.example.com/feed"}])


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "vigie.db")


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "digests"


@dataclass
class Seed:
    """One Item to collect, and the Assessment the fake LLM gives it (none if score is None)."""

    slug: str
    score: int | None = 8
    topic: str = "pratique-dev-data"
    age: timedelta = timedelta(days=2)
    kind: Kind = "article"
    words: int = 460
    duration_s: int | None = None
    host: str = "blog.example.com"
    audience: tuple[str, ...] = ("Dev", "Tech lead")

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.slug}"

    @property
    def title(self) -> str:
        return f"Title {self.slug}"


def seed(store: Store, *seeds: Seed, config: Config = CONFIG) -> None:
    """Store the Items with their text, then assess them through `vigie score`."""
    source = store.source("Example blog", "rss", "https://blog.example.com/feed")
    for s in seeds:
        published_at = NOW - s.age
        store.add_item(
            NewItem(
                source_id=source.id,
                url=s.url,
                title=s.title,
                published_at=published_at,
                first_seen_at=published_at,
                kind=s.kind,
                duration_s=s.duration_s,
            )
        )
        item_id = store.item_id(s.url)
        assert item_id is not None
        store.set_text(item_id, "word " * s.words, s.words)
    world = FakeWorld({})
    by_url = {s.url: s for s in seeds if s.score is not None}
    # Other Items get invalid LLM output, so they stay unassessed.
    world.replies = [
        reply(by_url[i.url]) if i.url in by_url else "not json" for i in store.items_to_assess()
    ]
    score(config, store, world, MODEL)


def reply(s: Seed) -> str:
    answer: dict[str, Any] = {
        "score": s.score,
        "summary_fr": f"Résumé de {s.slug}.",
        "audience": list(s.audience),
        "topic": s.topic,
        "reason": f"Raison de {s.slug}.",
    }
    return json.dumps(answer)


def written(report: DigestReport) -> str:
    """The Markdown file the run wrote."""
    assert report.path is not None
    return report.path.read_text(encoding="utf-8")


def shown_items(request: ChatRequest) -> list[dict[str, Any]]:
    """The Shortlist Items a ranking request shows the LLM, in order."""
    items: list[dict[str, Any]] = json.loads(request.messages[-1]["content"])
    return items


def keep_in_given_order(request: ChatRequest) -> str:
    """A ranking answer that keeps every Item shown, in Score order."""
    return json.dumps({"keep": [i["id"] for i in shown_items(request)], "dropped": []})


Ranking = str | Exception | Callable[[ChatRequest], str]


def world_at(now: datetime = NOW, ranking: Ranking = keep_in_given_order) -> FakeWorld:
    """The world at `now`, whose LLM answers the ranking call with `ranking`."""
    world = FakeWorld({}, now=now)
    world.replies = [ranking]
    return world


def item_id(store: Store, slug: str) -> int:
    [found] = [i.id for i in store.items() if i.title == Seed(slug).title]
    return found


def ranking_answer(
    store: Store,
    keep: list[str | int],
    duplicates: dict[str, str | int] | None = None,
    weak: list[str] | None = None,
) -> str:
    """A ranking answer naming Items by slug; an int is passed as a raw id."""

    def id_of(ref: str | int) -> int:
        return ref if isinstance(ref, int) else item_id(store, ref)

    dropped = [
        {"id": id_of(slug), "reason": "duplicate", "duplicate_of": id_of(of)}
        for slug, of in (duplicates or {}).items()
    ] + [{"id": id_of(slug), "reason": "weak", "duplicate_of": None} for slug in weak or []]
    return json.dumps({"keep": [id_of(ref) for ref in keep], "dropped": dropped})


def test_a_digest_with_one_pick_renders_the_message_and_is_written_after_the_build_date(
    store: Store, out: Path
) -> None:
    seed(store, Seed("parallel-agents", words=460, host="www.claude.com"))

    report = build_digest(CONFIG, store, world_at(), MODEL, out)

    assert report.path == out / "2026-10-29.md"
    assert message(written(report)) == (
        "# 🧭 Veille IA — semaine du 26/10\n"
        "\n"
        "Ce qu'il fallait retenir cette semaine sur l'IA dans le dev et la data.\n"
        "\n"
        "**1. Title parallel-agents**  📄 2 min · claude.com\n"
        "\n"
        "Résumé de parallel-agents.\n"
        "\n"
        "👥 Pour qui : Dev · Tech lead\n"
        "\n"
        "🔗 https://www.claude.com/parallel-agents\n"
        "\n"
        "Réagissez avec 1️⃣–5️⃣ sur les contenus qui vous ont été utiles (👎 si aucun),"
        " et discutons-en dans le fil.\n"
    )


def message(markdown: str) -> str:
    """The top half of the Markdown file: the message as it will be posted."""
    return markdown.split("\n---\n")[0]


def appendix(markdown: str) -> str:
    """The bottom half of the Markdown file: the review appendix, never sent."""
    return markdown.split("\n---\n")[1]


def pick_titles(markdown: str) -> list[str]:
    """The numbered Pick titles of the message, in order."""
    return [line.split("**")[1] for line in message(markdown).splitlines() if line.startswith("**")]


def test_items_outside_the_window_or_below_min_score_are_never_picked(
    store: Store, out: Path
) -> None:
    seed(
        store,
        Seed("in-window", score=6, age=timedelta(days=6)),
        Seed("too-old", score=10, age=timedelta(days=8)),
        Seed("too-weak", score=5),
        Seed("unassessed", score=None),
    )

    report = build_digest(CONFIG, store, world_at(), MODEL, out)

    assert pick_titles(written(report)) == ["1. Title in-window"]


def test_since_widens_the_window(store: Store, out: Path) -> None:
    seed(
        store,
        Seed("recent", score=7, age=timedelta(days=2)),
        Seed("last-week", score=9, age=timedelta(days=10)),
        Seed("last-month", score=9, age=timedelta(days=20)),
    )

    report = build_digest(CONFIG, store, world_at(), MODEL, out, since=timedelta(days=14))

    assert pick_titles(written(report)) == [
        "1. Title last-week",
        "2. Title recent",
    ]


def with_settings(**settings: int) -> Config:
    return CONFIG.model_copy(
        update={
            "settings": CONFIG.settings.model_copy(update=settings),
            "topics": [
                *CONFIG.topics,
                CONFIG.topics[0].model_copy(update={"id": "risques-et-couts"}),
            ],
        }
    )


def test_shortlist_size_topic_quota_and_pick_cap_hold_and_the_appendix_says_why(
    store: Store, out: Path
) -> None:
    config = with_settings(shortlist_size=6, max_per_topic=2, max_items=3)
    seed(
        store,
        Seed("dev-1", score=10),
        Seed("dev-2", score=9, age=timedelta(days=1)),
        # Same Score as dev-2 but older: ranked after it.
        Seed("dev-3", score=9, age=timedelta(days=3)),
        Seed("risk-1", score=8, topic="risques-et-couts"),
        Seed("risk-2", score=7, topic="risques-et-couts"),
        Seed("risk-3", score=7, topic="risques-et-couts", age=timedelta(days=4)),
        # Seventh by Score: not in the Shortlist.
        Seed("risk-4", score=6, topic="risques-et-couts"),
        config=config,
    )

    report = build_digest(config, store, world_at(), MODEL, out)

    markdown = written(report)
    assert pick_titles(markdown) == ["1. Title dev-1", "2. Title dev-2", "3. Title risk-1"]
    assert appendix(markdown) == (
        "\n"
        "## Review appendix (not sent)\n"
        "\n"
        "Window: 2026-10-22 12:00 UTC → 2026-10-29 12:00 UTC. Order: LLM ranking.\n"
        "\n"
        "1. **Title dev-1**: Score 10 · pratique-dev-data · Pick 1\n"
        "   Raison de dev-1.\n"
        "2. **Title dev-2**: Score 9 · pratique-dev-data · Pick 2\n"
        "   Raison de dev-2.\n"
        "3. **Title dev-3**: Score 9 · pratique-dev-data · dropped: Topic quota (2 Picks)\n"
        "   Raison de dev-3.\n"
        "4. **Title risk-1**: Score 8 · risques-et-couts · Pick 3\n"
        "   Raison de risk-1.\n"
        "5. **Title risk-2**: Score 7 · risques-et-couts · dropped: over the cap (3 Picks)\n"
        "   Raison de risk-2.\n"
        "6. **Title risk-3**: Score 7 · risques-et-couts · dropped: over the cap (3 Picks)\n"
        "   Raison de risk-3.\n"
    )


def pick_lines(markdown: str) -> list[str]:
    """The title line of each Pick, without its number and title."""
    return [line.split("**  ")[1] for line in message(markdown).splitlines() if line.startswith("**")]


def test_kind_icons_and_lengths(store: Store, out: Path) -> None:
    config = with_settings(max_per_topic=5)
    seed(
        store,
        Seed("exactly-one-minute", score=10, words=230),
        Seed("just-over-one-minute", score=9, words=231),
        # 42 min 17 s.
        Seed("talk", score=8, kind="video", duration_s=2537, host="www.youtube.com"),
        # 1 h 05 min 10 s.
        Seed("episode", score=7, kind="podcast", duration_s=3910, host="latent.space"),
        Seed("episode-without-duration", score=6, kind="podcast", host="latent.space"),
        config=config,
    )

    report = build_digest(config, store, world_at(), MODEL, out)

    assert pick_lines(written(report)) == [
        "📄 1 min · blog.example.com",
        "📄 2 min · blog.example.com",
        "🎥 42 min · youtube.com",
        "🎙️ 1 h 05 · latent.space",
        "🎙️ latent.space",
    ]


def test_a_second_run_in_the_same_week_re_renders_the_stored_digest(
    store: Store, out: Path
) -> None:
    seed(store, Seed("monday-pick", score=8))
    first = build_digest(CONFIG, store, world_at(), MODEL, out)
    built = written(first)
    # Collected and assessed later in the week.
    seed(store, Seed("later-pick", score=10, age=timedelta(days=0)))
    assert first.path is not None
    first.path.unlink()

    # Sunday 1 November: still ISO week 44.
    again = build_digest(CONFIG, store, world_at(NOW + timedelta(days=3)), MODEL, out)

    assert again.reused
    assert again.path == out / "2026-10-29.md"
    assert written(again) == built
    assert (again.week, again.picks) == ("2026-W44", 1)


@pytest.mark.parametrize(
    "force", [{"rebuild": True}, {"since": timedelta(days=7)}], ids=["--rebuild", "--since"]
)
def test_rebuild_or_since_recomputes_this_weeks_digest(
    store: Store, out: Path, force: dict[str, Any]
) -> None:
    seed(store, Seed("monday-pick", score=8))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    seed(store, Seed("later-pick", score=10, age=timedelta(days=0)))
    later = world_at(NOW + timedelta(days=3))

    rebuilt = build_digest(CONFIG, store, later, MODEL, out, **force)

    assert not rebuilt.reused
    assert rebuilt.path == out / "2026-11-01.md"
    assert pick_titles(written(rebuilt)) == [
        "1. Title later-pick",
        "2. Title monday-pick",
    ]
    # The rebuilt Digest is the one stored for the week from now on.
    again = build_digest(CONFIG, store, later, MODEL, out)
    assert again.reused
    assert written(again) == written(rebuilt)


def test_a_new_week_gets_a_new_digest(store: Store, out: Path) -> None:
    seed(store, Seed("last-week-pick", score=8))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    seed(store, Seed("this-week-pick", score=9, age=timedelta(days=-4)))

    # Tuesday 3 November, ISO week 45.
    report = build_digest(CONFIG, store, world_at(NOW + timedelta(days=5)), MODEL, out)

    assert not report.reused
    assert report.week == "2026-W45"
    markdown = written(report)
    assert markdown.startswith("# 🧭 Veille IA — semaine du 02/11\n")
    assert pick_titles(markdown) == ["1. Title this-week-pick", "2. Title last-week-pick"]


def test_zero_picks_stores_and_writes_nothing_and_explains_why(store: Store, out: Path) -> None:
    seed(
        store,
        Seed("weak", score=5),
        Seed("weaker", score=3),
        Seed("unassessed", score=None),
        Seed("old-but-good", score=9, age=timedelta(days=10)),
    )

    report = build_digest(CONFIG, store, world_at(), MODEL, out)

    assert report.path is None
    assert not out.exists()
    assert render_digest_summary(report).splitlines() == [
        "No Pick for 2026-W44: nothing stored or written.",
        "Items collected in the Window: 3",
        "Items assessed in the Window: 2",
        "Best Score: 5 (min_score is 6)",
    ]
    # Nothing stored: the next run builds again.
    seed(store, Seed("good", score=8))
    assert not build_digest(CONFIG, store, world_at(), MODEL, out).reused


def test_zero_picks_in_an_empty_window_without_asking_the_llm(store: Store, out: Path) -> None:
    world = world_at()

    report = build_digest(CONFIG, store, world, MODEL, out)

    assert world.chats == []

    assert render_digest_summary(report).splitlines() == [
        "No Pick for 2026-W44: nothing stored or written.",
        "Items collected in the Window: 0",
        "Items assessed in the Window: 0",
        "Best Score: none",
    ]


def test_summary_of_a_built_and_of_a_reused_digest(store: Store, out: Path) -> None:
    seed(store, Seed("one", score=9), Seed("two", score=8))

    built = render_digest_summary(build_digest(CONFIG, store, world_at(), MODEL, out))
    reused = render_digest_summary(build_digest(CONFIG, store, world_at(), MODEL, out))

    assert built == f"Digest for 2026-W44 built with 2 Picks: {out / '2026-10-29.md'}"
    assert reused.splitlines() == [
        f"Digest for 2026-W44 already built, with 2 Picks: {out / '2026-10-29.md'}",
        "Pass --rebuild to build it again.",
    ]


def test_picks_follow_the_llm_ranking_asked_with_thinking_on_and_a_json_schema(
    store: Store, out: Path
) -> None:
    seed(
        store,
        Seed("best-score", score=10, host="www.claude.com"),
        Seed("middle", score=8),
        Seed("lowest", score=7),
    )
    world = world_at(ranking=ranking_answer(store, keep=["lowest", "best-score", "middle"]))

    report = build_digest(CONFIG, store, world, MODEL, out)

    assert pick_titles(written(report)) == [
        "1. Title lowest",
        "2. Title best-score",
        "3. Title middle",
    ]
    [request] = world.chats
    assert (request.model, request.think) == (MODEL, True)
    # The model is not loaded: no context size is sent (ADR 0001).
    assert request.num_ctx is None
    assert request.schema is not None
    assert set(request.schema["required"]) == {"keep", "dropped"}
    # Shown in Score order.
    assert shown_items(request)[0] == {
        "id": item_id(store, "best-score"),
        "title": "Title best-score",
        "domain": "claude.com",
        "kind": "article",
        "topic": "pratique-dev-data",
        "score": 10,
        "summary": "Résumé de best-score.",
        "reason": "Raison de best-score.",
    }
    assert [i["title"] for i in shown_items(request)[1:]] == ["Title middle", "Title lowest"]


def test_code_applies_quotas_and_the_cap_in_llm_order_and_ignores_unknown_or_repeated_ids(
    store: Store, out: Path
) -> None:
    config = with_settings(max_per_topic=1, max_items=2)
    seed(
        store,
        Seed("dev-best", score=10),
        Seed("dev-second", score=9),
        Seed("risk-best", score=8, topic="risques-et-couts"),
        Seed("risk-second", score=7, topic="risques-et-couts"),
        Seed("forgotten", score=6),
        config=config,
    )
    keep: list[str | int] = [999, "risk-best", "risk-best", "risk-second", "dev-best", "dev-second"]
    world = world_at(ranking=ranking_answer(store, keep=keep))

    report = build_digest(config, store, world, MODEL, out)

    markdown = written(report)
    assert pick_titles(markdown) == ["1. Title risk-best", "2. Title dev-best"]
    assert appendix(markdown) == (
        "\n"
        "## Review appendix (not sent)\n"
        "\n"
        "Window: 2026-10-22 12:00 UTC → 2026-10-29 12:00 UTC. Order: LLM ranking.\n"
        "\n"
        "1. **Title risk-best**: Score 8 · risques-et-couts · Pick 1\n"
        "   Raison de risk-best.\n"
        "2. **Title risk-second**: Score 7 · risques-et-couts · dropped: Topic quota (1 Picks)\n"
        "   Raison de risk-second.\n"
        "3. **Title dev-best**: Score 10 · pratique-dev-data · Pick 2\n"
        "   Raison de dev-best.\n"
        "4. **Title dev-second**: Score 9 · pratique-dev-data · dropped: over the cap (2 Picks)\n"
        "   Raison de dev-second.\n"
        "5. **Title forgotten**: Score 6 · pratique-dev-data · dropped: not ranked\n"
        "   Raison de forgotten.\n"
    )


def test_duplicates_and_weak_items_the_llm_drops_are_dropped_and_the_appendix_says_why(
    store: Store, out: Path
) -> None:
    seed(
        store,
        Seed("announce-first-blog", score=10),
        Seed("announce-second-blog", score=9),
        Seed("hype", score=8),
        Seed("orphan", score=7),
        Seed("solid", score=6),
    )
    answer = ranking_answer(
        store,
        keep=["announce-first-blog", "solid"],
        # Dropping a kept Item is ignored.
        duplicates={"announce-second-blog": "announce-first-blog", "orphan": 999},
        weak=["hype", "solid"],
    )

    report = build_digest(CONFIG, store, world_at(ranking=answer), MODEL, out)

    markdown = written(report)
    assert pick_titles(markdown) == ["1. Title announce-first-blog", "2. Title solid"]
    assert [line for line in appendix(markdown).splitlines() if line[:1].isdigit()] == [
        "1. **Title announce-first-blog**: Score 10 · pratique-dev-data · Pick 1",
        "2. **Title solid**: Score 6 · pratique-dev-data · Pick 2",
        "3. **Title announce-second-blog**: Score 9 · pratique-dev-data"
        ' · dropped: Duplicate of "Title announce-first-blog"',
        "4. **Title hype**: Score 8 · pratique-dev-data · dropped: weak",
        "5. **Title orphan**: Score 7 · pratique-dev-data"
        " · dropped: Duplicate of an unknown Item",
    ]


OLLAMA_DOWN = "Ollama at gpu-box: ConnectError: refused"


@pytest.mark.parametrize(
    ("ranking", "why"),
    [
        (LLMError(OLLAMA_DOWN), OLLAMA_DOWN),
        ("I would pick the first one.", "unusable output"),
        (json.dumps({"keep": "all", "dropped": []}), "unusable output"),
        # Every kept id unknown: the LLM did not answer about this Shortlist.
        (json.dumps({"keep": [998, 999], "dropped": []}), "unusable output"),
    ],
    ids=["LLM error", "not JSON", "wrong shape", "unknown ids only"],
)
def test_a_failed_ranking_falls_back_to_score_order_and_the_appendix_says_so(
    store: Store, out: Path, ranking: Ranking, why: str
) -> None:
    seed(store, Seed("first", score=9), Seed("second", score=8))

    report = build_digest(CONFIG, store, world_at(ranking=ranking), MODEL, out)

    markdown = written(report)
    assert pick_titles(markdown) == ["1. Title first", "2. Title second"]
    assert (
        "Window: 2026-10-22 12:00 UTC → 2026-10-29 12:00 UTC."
        f" Order: Score, as the ranking failed ({why}).\n"
    ) in appendix(markdown)


def test_a_ranking_that_keeps_nothing_means_zero_picks(store: Store, out: Path) -> None:
    seed(store, Seed("hype", score=8), Seed("news", score=7))
    answer = ranking_answer(store, keep=[], weak=["hype", "news"])

    report = build_digest(CONFIG, store, world_at(ranking=answer), MODEL, out)

    assert report.path is None
    assert not out.exists()
    assert render_digest_summary(report).splitlines() == [
        "No Pick for 2026-W44: nothing stored or written.",
        "Items collected in the Window: 2",
        "Items assessed in the Window: 2",
        "Best Score: 8 (min_score is 6)",
        "The ranking kept none of the 2 Shortlist Items.",
    ]


def test_the_ranking_reuses_the_loaded_context_size_and_leaves_out_items_that_do_not_fit(
    store: Store, out: Path
) -> None:
    config = with_settings(max_per_topic=5)
    # About 2,000 characters each, as the slug is in the title, summary and reason: they
    # cannot all fit in 4,000 tokens with room left to think.
    seeds = [Seed(f"long-{n}-" + "x" * 600, score=10 - n // 2) for n in range(8)]
    seed(store, *seeds, config=config)
    world = world_at()
    world.loaded_ctx = 4000

    report = build_digest(config, store, world, MODEL, out)

    [request] = world.chats
    assert request.num_ctx == 4000
    shown = [i["title"] for i in shown_items(request)]
    # The best-scored Items, as many as fit, with at least 1,000 tokens left to answer.
    assert shown == [s.title for s in seeds[: len(shown)]]
    assert 1 <= len(shown) < len(seeds)
    assert sum(len(m["content"]) for m in request.messages) <= (4000 - 1000) * 4
    left_out = appendix(written(report)).count(
        "dropped: not ranked (left out to fit the context size)"
    )
    assert left_out == len(seeds) - len(shown)


@pytest.mark.parametrize(
    ("loaded_ctx", "why"),
    [
        (LLMError(OLLAMA_DOWN), OLLAMA_DOWN),
        (2048, "no Item fits the context size"),
    ],
    ids=["Ollama unreachable", "context too small"],
)
def test_the_ranking_falls_back_to_score_order_before_any_call(
    store: Store, out: Path, loaded_ctx: int | Exception, why: str
) -> None:
    seed(store, Seed("first", score=9), Seed("second", score=8))
    world = world_at()
    world.loaded_ctx = loaded_ctx

    report = build_digest(CONFIG, store, world, MODEL, out)

    assert world.chats == []
    markdown = written(report)
    assert pick_titles(markdown) == ["1. Title first", "2. Title second"]
    assert f"Order: Score, as the ranking failed ({why})." in appendix(markdown)
