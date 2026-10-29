import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import NOW
from test_digest import CONFIG, MODEL, Seed, pick_titles, seed, world_at, written

from vigie.assess import score
from vigie.digest import build_digest, render_digest_summary
from vigie.send import render_send_summary, send_digest
from vigie.store import Store, open_store
from vigie.world import SlackError


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "vigie.db")


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "digests"


@dataclass
class Titled(Seed):
    """A Seed with a title of its own."""

    name: str = ""

    @property
    def title(self) -> str:
        return self.name or super().title


FOOTER = (
    "Réagissez avec 1️⃣–5️⃣ sur les contenus qui vous ont été utiles (👎 si aucun),"
    " et discutons-en dans le fil."
)


def test_send_posts_the_message_as_block_kit_without_the_appendix(
    store: Store, out: Path
) -> None:
    seed(
        store,
        Titled("evals", score=9, name="Evals <for> agents & teams", host="www.claude.com"),
        Seed("talk", score=8, kind="video", duration_s=2537, audience=("Data",)),
    )
    build_digest(CONFIG, store, world_at(), MODEL, out)
    world = world_at()

    send_digest(store, world)

    assert world.slack_posts == [
        {
            "text": "🧭 Veille IA — semaine du 26/10",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "🧭 Veille IA — semaine du 26/10"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Ce qu'il fallait retenir cette semaine sur l'IA"
                        " dans le dev et la data.",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*1. Evals &lt;for&gt; agents &amp; teams*  📄 2 min · claude.com\n"
                        "Résumé de evals.\n"
                        "👥 Pour qui : Dev · Tech lead\n"
                        "🔗 https://www.claude.com/evals",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*2. Title talk*  🎥 42 min · blog.example.com\n"
                        "Résumé de talk.\n"
                        "👥 Pour qui : Data\n"
                        "🔗 https://blog.example.com/talk",
                    },
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": FOOTER}},
            ],
        }
    ]


def test_send_without_a_stored_digest_refuses_and_points_to_vigie_digest(store: Store) -> None:
    world = world_at()

    report = send_digest(store, world)

    assert world.slack_posts == []
    assert not report.sent
    assert render_send_summary(report) == (
        "No Digest stored for 2026-W44: run `vigie digest` first and review it."
    )


def test_a_second_send_in_the_same_week_refuses_unless_forced(store: Store, out: Path) -> None:
    seed(store, Seed("monday-pick"))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    world = world_at()
    first = send_digest(store, world)
    # Sunday 1 November: still ISO week 44.
    world.current_time = NOW + timedelta(days=3)

    again = send_digest(store, world)
    forced = send_digest(store, world, force=True)

    assert render_send_summary(first) == "Digest for 2026-W44 sent to Slack."
    assert not again.sent
    assert render_send_summary(again).splitlines() == [
        "Digest for 2026-W44 already sent to slack on 2026-10-29 12:00 UTC.",
        "Pass --force to send it again.",
    ]
    assert forced.sent
    assert len(world.slack_posts) == 2
    assert world.slack_posts[1] == world.slack_posts[0]


def test_a_webhook_failure_reports_the_error_and_leaves_the_digest_unsent(
    store: Store, out: Path
) -> None:
    seed(store, Seed("monday-pick"))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    world = world_at()
    world.slack_error = SlackError("Slack webhook answered HTTP 404: no_service")

    failed = send_digest(store, world)
    world.slack_error = None
    retried = send_digest(store, world)

    assert not failed.sent
    assert render_send_summary(failed).splitlines() == [
        "Could not send the Digest for 2026-W44: Slack webhook answered HTTP 404: no_service",
        "It is not marked as sent.",
    ]
    assert retried.sent
    assert len(world.slack_posts) == 1


def test_a_digest_stored_before_slack_was_supported_asks_for_a_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "vigie.db"
    # A database written before the Slack payload was stored with each Digest.
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE digests (id INTEGER PRIMARY KEY, week TEXT NOT NULL UNIQUE,"
            " window_start TEXT NOT NULL, window_end TEXT NOT NULL, pick_ids TEXT NOT NULL,"
            " rendered_md TEXT NOT NULL, built_at TEXT NOT NULL);"
            "INSERT INTO digests (week, window_start, window_end, pick_ids, rendered_md, built_at)"
            " VALUES ('2026-W44', '2026-10-22T12:00:00+00:00', '2026-10-29T12:00:00+00:00',"
            " '[1]', '# Digest', '2026-10-29T12:00:00+00:00');"
        )
    db.close()
    store = open_store(path)
    world = world_at()

    report = send_digest(store, world)

    assert world.slack_posts == []
    assert render_send_summary(report) == (
        "The Digest stored for 2026-W44 has no Slack payload yet:"
        " run `vigie digest --rebuild` and review it."
    )


def test_picks_of_a_digest_sent_for_an_earlier_week_are_never_picked_again(
    store: Store, out: Path
) -> None:
    seed(store, Seed("sent-last-week", score=9))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    send_digest(store, world_at())
    seed(store, Seed("this-week-pick", score=7, age=timedelta(days=-4)))

    # Tuesday 3 November, ISO week 45: sent-last-week is still in the Window.
    report = build_digest(CONFIG, store, world_at(NOW + timedelta(days=5)), MODEL, out)

    assert pick_titles(written(report)) == ["1. Title this-week-pick"]


def test_send_posts_the_payload_stored_at_build_even_after_a_rescore(
    store: Store, out: Path
) -> None:
    seed(store, Seed("pick", score=8))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    rescoring = world_at()
    rescoring.replies = [
        json.dumps(
            {
                "score": 10,
                "summary_fr": "Un autre résumé.",
                "audience": ["Manager"],
                "topic": "pratique-dev-data",
                "reason": "Autre raison.",
            }
        )
    ]
    score(CONFIG, store, rescoring, MODEL, rescore=True)
    world = world_at()

    send_digest(store, world)

    [payload] = world.slack_posts
    assert payload["blocks"][2]["text"]["text"] == (
        "*1. Title pick*  📄 2 min · blog.example.com\n"
        "Résumé de pick.\n"
        "👥 Pour qui : Dev · Tech lead\n"
        "🔗 https://blog.example.com/pick"
    )


@pytest.mark.parametrize(
    "force", [{"rebuild": True}, {"since": timedelta(days=14)}], ids=["--rebuild", "--since"]
)
def test_a_sent_digest_is_never_rebuilt_so_its_picks_stay_out_of_later_digests(
    store: Store, out: Path, force: dict[str, Any]
) -> None:
    seed(store, Seed("sent-pick", score=8))
    build_digest(CONFIG, store, world_at(), MODEL, out)
    send_digest(store, world_at())
    seed(store, Seed("better-pick", score=10, age=timedelta(days=0)))

    refused = build_digest(CONFIG, store, world_at(), MODEL, out, **force)
    # Tuesday 3 November, ISO week 45: sent-pick is still in the Window.
    next_week = build_digest(CONFIG, store, world_at(NOW + timedelta(days=5)), MODEL, out)

    assert render_digest_summary(refused) == (
        "Digest for 2026-W44 already sent to slack on 2026-10-29 12:00 UTC: not rebuilt."
    )
    assert pick_titles(written(next_week)) == ["1. Title better-pick"]


def test_summaries_are_escaped_for_slack(store: Store, out: Path) -> None:
    seed(store, Seed("costs", score=None))
    assessing = world_at()
    assessing.replies = [
        json.dumps(
            {
                "score": 8,
                "summary_fr": "Coûts <réels> & cachés des agents.",
                "audience": ["Manager"],
                "topic": "pratique-dev-data",
                "reason": "Raison.",
            }
        )
    ]
    score(CONFIG, store, assessing, MODEL)
    build_digest(CONFIG, store, world_at(), MODEL, out)
    world = world_at()

    send_digest(store, world)

    [payload] = world.slack_posts
    assert payload["blocks"][2]["text"]["text"].splitlines()[1] == (
        "Coûts &lt;réels&gt; &amp; cachés des agents."
    )
