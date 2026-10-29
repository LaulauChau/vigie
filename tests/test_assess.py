import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import NOW, FakeWorld, make_config, ok

from vigie.assess import collect_and_score, render_score_summary, score
from vigie.config import Config, Topic
from vigie.store import Item, NewItem, Store, open_store
from vigie.world import ChatReply, LLMError

MODEL = "qwen3.8"
CONFIG = make_config([{"name": "Example blog", "type": "rss", "url": "https://blog.example.com/feed"}])
ARTICLE_TEXT = "For the last three months our team has run coding agents in parallel. " * 20


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "vigie.db")


def add_item(
    store: Store,
    url: str,
    text: str | None = ARTICLE_TEXT,
    published_at: datetime = NOW - timedelta(days=2),
) -> Item:
    source = store.source("Example blog", "rss", "https://blog.example.com/feed")
    store.add_item(
        NewItem(
            source_id=source.id,
            url=url,
            title=f"Title of {url}",
            published_at=published_at,
            first_seen_at=published_at,
            kind="article",
            duration_s=None,
        )
    )
    [item] = [i for i in store.items() if i.url == url]
    if text is not None:
        store.set_text(item.id, text, len(text.split()))
    return item


def reply(**overrides: Any) -> str:
    answer = {
        "score": 8,
        "summary_fr": "L'équipe fait tourner des agents en parallèle. Elle mesure le gain.",
        "audience": ["Dev", "Tech lead"],
        "topic": "pratique-dev-data",
        "reason": "Retour d'expérience concret et chiffré.",
    }
    answer.update(overrides)
    return json.dumps(answer)


def test_pending_items_get_an_assessment_and_a_second_run_calls_no_llm(store: Store) -> None:
    item = add_item(store, "https://blog.example.com/parallel-agents")
    # Skipped Items and Items not extracted yet have no text to assess.
    add_item(store, "https://blog.example.com/teaser", text=None)
    world = FakeWorld({})
    world.replies = [reply()]

    report = score(CONFIG, store, world, MODEL)

    assert report.assessed == 1
    [assessment] = store.assessments()
    assert assessment.item_id == item.id
    assert assessment.score == 8
    assert assessment.summary_fr.startswith("L'équipe fait tourner des agents")
    assert assessment.audience == ("Dev", "Tech lead")
    assert assessment.topic == "pratique-dev-data"
    assert assessment.reason == "Retour d'expérience concret et chiffré."
    assert assessment.model == MODEL
    assert assessment.assessed_at == NOW
    assert len(world.chats) == 1

    world.chats.clear()
    report = score(CONFIG, store, world, MODEL)

    assert report.assessed == 0
    assert world.chats == []


@pytest.mark.parametrize(
    "bad",
    [
        "Voici mon évaluation : 8/10",
        reply(score=11),
        reply(audience=[]),
        reply(audience=["Dev", "Product owner"]),
        reply(topic="actualite-modeles"),
        reply(summary_fr=""),
    ],
    ids=["not json", "score above 10", "no audience", "unknown role", "unknown topic", "empty"],
)
def test_invalid_output_leaves_the_item_unscored_and_the_next_run_retries_it(
    store: Store, bad: str
) -> None:
    add_item(store, "https://blog.example.com/parallel-agents")
    world = FakeWorld({})
    world.replies = [bad]

    report = score(CONFIG, store, world, MODEL)

    assert (report.assessed, report.invalid) == (0, 1)
    assert store.assessments() == []

    world.replies = [reply()]
    report = score(CONFIG, store, world, MODEL)

    assert (report.assessed, report.invalid) == (1, 0)
    assert len(store.assessments()) == 1


def test_scoring_requests_have_thinking_off_low_temperature_and_the_output_schema(
    store: Store,
) -> None:
    add_item(store, "https://blog.example.com/parallel-agents")
    add_item(store, "https://blog.example.com/reviewing-generated-code")
    world = FakeWorld({})
    world.replies = [reply(), reply()]

    score(CONFIG, store, world, MODEL)

    assert len(world.chats) == 2
    for request in world.chats:
        assert request.model == MODEL
        assert request.think is False
        assert request.temperature is not None and request.temperature <= 0.2
        assert request.schema is not None
        assert set(request.schema["required"]) == {
            "score",
            "summary_fr",
            "audience",
            "topic",
            "reason",
        }
        assert request.schema["properties"]["topic"]["enum"] == ["pratique-dev-data"]


def prompt_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(m["content"]) for m in messages)


def test_context_size_sent_is_the_loaded_one_and_the_text_is_truncated_to_fit(
    store: Store,
) -> None:
    beginning = "Our team ran parallel coding agents for three months. "
    ending = "THE VERY END OF THE ARTICLE."
    add_item(store, "https://blog.example.com/long", text=beginning + "x " * 20_000 + ending)
    world = FakeWorld({})
    world.loaded_ctx = 3000
    world.replies = [reply()]

    score(CONFIG, store, world, MODEL)

    [request] = world.chats
    assert request.num_ctx == 3000
    [_, user] = request.messages
    assert beginning in user["content"]
    assert ending not in user["content"]
    # About 4 characters per token, with room left for the answer.
    assert 2000 * 4 <= prompt_chars(request.messages) <= (3000 - 256) * 4


def test_no_context_size_is_sent_when_the_model_is_not_loaded_and_4096_tokens_are_assumed(
    store: Store,
) -> None:
    add_item(store, "https://blog.example.com/long", text="word " * 20_000)
    add_item(store, "https://blog.example.com/short")
    world = FakeWorld({})
    world.loaded_ctx = None
    world.replies = [reply(), reply()]

    score(CONFIG, store, world, MODEL)

    long, short = world.chats
    assert long.num_ctx is None
    assert short.num_ctx is None
    assert 3000 * 4 <= prompt_chars(long.messages) <= (4096 - 256) * 4
    # A short text is sent whole.
    assert ARTICLE_TEXT in short.messages[1]["content"]


def test_rescore_replaces_the_assessments_in_the_window_without_fetching(store: Store) -> None:
    recent = add_item(store, "https://blog.example.com/recent", published_at=NOW - timedelta(days=2))
    older = add_item(store, "https://blog.example.com/older", published_at=NOW - timedelta(days=10))
    world = FakeWorld({})
    world.replies = [reply(score=5), reply(score=5)]
    score(CONFIG, store, world, MODEL)
    world.replies = [reply(score=9)]

    # The Window is window_days (7) by default.
    report = score(CONFIG, store, world, MODEL, rescore=True)

    assert report.assessed == 1
    scores = {a.item_id: a.score for a in store.assessments()}
    assert scores == {recent.id: 9, older.id: 5}
    assert world.fetches == []

    world.replies = [reply(score=2), reply(score=3)]
    report = score(CONFIG, store, world, MODEL, rescore=True, since=timedelta(days=14))

    assert report.assessed == 2
    scores = {a.item_id: a.score for a in store.assessments()}
    assert scores == {recent.id: 2, older.id: 3}
    assert world.fetches == []


def with_topic_description(description: str) -> Config:
    return CONFIG.model_copy(
        update={"topics": [Topic(id="pratique-dev-data", description=description)]}
    )


def test_changing_a_topic_description_changes_the_stored_prompt_hash(store: Store) -> None:
    add_item(store, "https://blog.example.com/parallel-agents")
    world = FakeWorld({})
    world.replies = [reply(), reply(), reply()]
    score(with_topic_description("Pratique du dev."), store, world, MODEL)
    [first] = store.assessments()

    score(with_topic_description("Pratique du dev."), store, world, MODEL, rescore=True)
    [same] = store.assessments()
    score(with_topic_description("Pratique du dev et de la data."), store, world, MODEL, rescore=True)
    [changed] = store.assessments()

    assert same.prompt_hash == first.prompt_hash
    assert changed.prompt_hash != first.prompt_hash


def test_prompt_carries_the_company_context_the_topics_and_the_item(store: Store) -> None:
    add_item(store, "https://blog.example.com/parallel-agents")
    world = FakeWorld({})
    world.replies = [reply()]

    score(with_topic_description("Agents de code au quotidien."), store, world, MODEL)

    [request] = world.chats
    system, user = request.messages
    assert system["role"] == "system"
    assert "Cellule IA" in system["content"]
    assert "pratique-dev-data: Agents de code au quotidien." in system["content"]
    assert "benchmarks" in system["content"]
    assert "French" in system["content"]
    assert user["role"] == "user"
    assert "Title of https://blog.example.com/parallel-agents" in user["content"]
    assert ARTICLE_TEXT in user["content"]


def test_unreachable_ollama_is_reported_and_the_items_wait_for_the_next_run(
    store: Store,
) -> None:
    add_item(store, "https://blog.example.com/parallel-agents")
    world = FakeWorld({})
    world.loaded_ctx = LLMError("Ollama at http://ollama.internal:11434: ConnectionError")

    report = score(CONFIG, store, world, MODEL)

    assert report.error == "Ollama at http://ollama.internal:11434: ConnectionError"
    assert report.assessed == 0
    assert store.assessments() == []

    world.loaded_ctx = None
    world.replies = [reply()]
    report = score(CONFIG, store, world, MODEL)

    assert (report.assessed, report.error) == (1, None)


def test_ollama_failing_mid_run_keeps_the_assessments_already_made(store: Store) -> None:
    for slug in ("one", "two", "three"):
        add_item(store, f"https://blog.example.com/{slug}")
    world = FakeWorld({})
    world.replies = [reply(), LLMError("Ollama at localhost:11434: ReadTimeout")]

    report = score(CONFIG, store, world, MODEL)

    assert report.assessed == 1
    assert report.error == "Ollama at localhost:11434: ReadTimeout"
    assert len(world.chats) == 2
    assert len(store.assessments()) == 1


def test_a_long_model_load_time_logs_a_possible_reload(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    add_item(store, "https://blog.example.com/one")
    add_item(store, "https://blog.example.com/two")
    world = FakeWorld({})
    world.replies = [
        ChatReply(text=reply(), load_seconds=0.05),
        ChatReply(text=reply(), load_seconds=12.0),
    ]

    with caplog.at_level(logging.WARNING):
        score(CONFIG, store, world, MODEL)

    [warning] = caplog.records
    assert warning.levelno == logging.WARNING
    assert "reload" in warning.getMessage()
    assert "12.0" in warning.getMessage()


def test_summary_counts_assessed_and_invalid_items_and_shows_the_error(store: Store) -> None:
    for slug in ("one", "two", "three"):
        add_item(store, f"https://blog.example.com/{slug}")
    world = FakeWorld({})
    world.replies = [reply(), "garbage", LLMError("Ollama at localhost:11434: ReadTimeout")]

    summary = render_score_summary(score(CONFIG, store, world, MODEL))

    assert summary.splitlines() == [
        "Assessed Items: 1",
        "Invalid LLM output, retried next run: 1",
        "Ollama error: Ollama at localhost:11434: ReadTimeout",
        "Items left unscored; run `vigie score` once Ollama is reachable.",
    ]


def blog_world() -> FakeWorld:
    """The blog feed of fixture blog.rss.xml, with its three article pages."""
    return FakeWorld(
        {
            "https://blog.example.com/feed": ok("blog.rss.xml"),
            "https://blog.example.com/parallel-agents": ok("article.html"),
            "https://blog.example.com/reviewing-generated-code": ok("article.html"),
            "https://blog.example.com/mcp-data-team": ok("article.html"),
        }
    )


def test_collect_assesses_the_new_items_after_collecting(store: Store) -> None:
    world = blog_world()
    world.replies = [reply(), reply(), reply()]

    collected, scored = collect_and_score(CONFIG, store, world, MODEL)

    assert collected.new == 3
    assert scored.assessed == 3
    assert {a.item_id for a in store.assessments()} == {i.id for i in store.items()}

    collected, scored = collect_and_score(CONFIG, store, world, MODEL)

    assert (collected.new, scored.assessed) == (0, 0)
    assert len(world.chats) == 3


def test_collect_with_ollama_down_saves_the_items_and_reports_the_error(store: Store) -> None:
    world = blog_world()
    world.loaded_ctx = LLMError("Ollama at http://ollama.internal:11434: ConnectionError")

    collected, scored = collect_and_score(CONFIG, store, world, MODEL)

    assert collected.new == 3
    assert len(store.items()) == 3
    assert scored.error == "Ollama at http://ollama.internal:11434: ConnectionError"
    assert store.assessments() == []

    # Later, `vigie score` picks them up without fetching anything.
    world.loaded_ctx = None
    world.replies = [reply(), reply(), reply()]
    world.fetches.clear()
    report = score(CONFIG, store, world, MODEL)

    assert report.assessed == 3
    assert world.fetches == []


def test_summary_of_a_run_with_nothing_to_assess(store: Store) -> None:
    summary = render_score_summary(score(CONFIG, store, FakeWorld({}), MODEL))

    assert summary.splitlines() == ["Assessed Items: 0"]
