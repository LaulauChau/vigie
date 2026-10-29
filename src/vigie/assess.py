"""`vigie score`: Assessments of Items by the shared Ollama model."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from vigie.collect import CollectReport, collect
from vigie.config import Config, Topic
from vigie.store import Assessment, Item, Store
from vigie.world import LLMError, Message, World

Audience = Literal["Dev", "Data", "Tech lead", "Cellule IA", "Manager"]

TEMPERATURE = 0.1
# ADR 0001: the context size is never chosen by Vigie, only reused, so Item text is
# truncated to fit it. Estimated at about 4 characters per token.
CHARS_PER_TOKEN = 4
# Assumed when the model is not loaded and its context size is unknown.
DEFAULT_CONTEXT_TOKENS = 4096
# Kept free for the JSON answer: a 2–3 sentence summary and a one-sentence reason.
ANSWER_TOKENS = 512
# A model already in memory loads in a few milliseconds; seconds mean it was (re)loaded.
RELOAD_WARNING_SECONDS = 1.0

log = logging.getLogger(__name__)

# Written in English, asking for French output.
SYSTEM_TEMPLATE = """\
You assess content for a weekly digest read by the staff of a French IT services company \
(ESN) focused on data and software.

# Company context

The company is running an AI transformation plan. A dedicated unit, the Cellule IA, hunts for \
AI use cases in internal and client projects. Readers are developers, data engineers and \
analysts, tech leads, the Cellule IA and managers. They want to learn how practitioners \
actually use AI in day-to-day dev and data work.

# Topics

{topics}

# What to value

- Concrete workflows: parallel agents, git worktrees, plan mode, context files (CLAUDE.md, \
AGENTS.md), skills, hooks, MCP servers, review of generated code.
- Experience reports with measured results.
- Adoption of AI by a team or a company.
- Business use cases that transfer to an ESN's data and software clients.
- Limits and risks of AI: security, cost, quality.
- Use of local or self-hosted models.

# What to penalize hard

Give 3 or less to: new model announcements, benchmarks, funding, business news, hype without \
substance, purely promotional content, generic beginner tutorials.

# Your answer

Answer with a JSON object only. Write every text field in French.
- score: integer from 0 (useless to the readers) to 10 (a must-read).
- summary_fr: 2 to 3 factual sentences in French on what the reader concretely learns.
- audience: the roles the content is useful for, at least one of: Dev, Data, Tech lead, \
Cellule IA, Manager.
- topic: the id of the one Topic that fits best.
- reason: one sentence in French explaining the score.
"""

USER_TEMPLATE = """\
Title: {title}
Source: {source}
Kind: {kind}
URL: {url}

Text:
{text}"""


# What the model must answer. Its JSON schema is also passed to Ollama as `format`, so
# it has no docstring: that would land in the schema.
class AssessmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=10)
    summary_fr: str = Field(min_length=1)
    audience: list[Audience] = Field(min_length=1)
    # One of the configured Topic ids, passed in the validation context.
    topic: str
    reason: str = Field(min_length=1)

    @field_validator("audience")
    @classmethod
    def _as_subset(cls, v: list[Audience]) -> list[Audience]:
        # The schema cannot stop the model from repeating a role; that is harmless.
        return list(dict.fromkeys(v))

    @field_validator("topic")
    @classmethod
    def _known_topic(cls, v: str, info: ValidationInfo) -> str:
        topics = (info.context or {}).get("topics", ())
        if v not in topics:
            raise ValueError(f"unknown Topic {v!r}")
        return v


@dataclass
class ScoreReport:
    assessed: int = 0
    # Items whose LLM output failed validation; they are retried next run.
    invalid: int = 0
    # Ollama could not be reached; the Items left unscored wait for the next run.
    error: str | None = None


def score(
    config: Config,
    store: Store,
    world: World,
    model: str,
    *,
    rescore: bool = False,
    since: timedelta | None = None,
) -> ScoreReport:
    """Assess every Item that has text and no Assessment yet.

    With `rescore`, assess again every Item with text in the Window instead: the last
    `since`, or `window_days` by default. An earlier Assessment is replaced only by a
    valid one. Nothing is fetched.
    """
    report = ScoreReport()
    if rescore:
        window = since or timedelta(days=config.settings.window_days)
        items = store.items_with_text_since(world.now() - window)
    else:
        items = store.items_to_assess()
    if not items:
        return report
    try:
        # Read once, before scoring, and sent back as is (ADR 0001).
        num_ctx = world.loaded_context_size(model)
        prompt = _Prompt(config.topics, num_ctx or DEFAULT_CONTEXT_TOKENS)
        for item in items:
            _assess(item, prompt, model, num_ctx, store, world, report)
    except LLMError as e:
        report.error = str(e)
    return report


def collect_and_score(
    config: Config, store: Store, world: World, model: str
) -> tuple[CollectReport, ScoreReport]:
    """`vigie collect`: collect new Items, then assess the pending ones.

    Collected Items are saved before scoring starts, so they survive Ollama being down.
    """
    return collect(config, store, world), score(config, store, world, model)


def render_score_summary(report: ScoreReport) -> str:
    lines = [f"Assessed Items: {report.assessed}"]
    if report.invalid:
        lines.append(f"Invalid LLM output, retried next run: {report.invalid}")
    if report.error:
        lines.append(f"Ollama error: {' '.join(report.error.split())}")
        lines.append("Items left unscored; run `vigie score` once Ollama is reachable.")
    return "\n".join(lines)


class _Prompt:
    def __init__(self, topics: list[Topic], context_tokens: int) -> None:
        self.topic_ids = [t.id for t in topics]
        self.system = SYSTEM_TEMPLATE.format(
            topics="\n".join(f"- {t.id}: {t.description}" for t in topics)
        )
        self.hash = hashlib.sha256(f"{self.system}\n{USER_TEMPLATE}".encode()).hexdigest()
        schema = AssessmentOutput.model_json_schema()
        schema["properties"]["topic"]["enum"] = self.topic_ids
        self.schema: dict[str, Any] = schema
        self._budget_chars = (context_tokens - ANSWER_TOKENS) * CHARS_PER_TOKEN

    def messages(self, item: Item) -> list[Message]:
        def user(text: str) -> str:
            return USER_TEMPLATE.format(
                title=item.title, source=item.source, kind=item.kind, url=item.url, text=text
            )

        room = self._budget_chars - len(self.system) - len(user(""))
        text = (item.content or "")[: max(room, 0)]
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": user(text)},
        ]


def _assess(
    item: Item,
    prompt: _Prompt,
    model: str,
    num_ctx: int | None,
    store: Store,
    world: World,
    report: ScoreReport,
) -> None:
    reply = world.chat(
        model,
        prompt.messages(item),
        schema=prompt.schema,
        think=False,
        num_ctx=num_ctx,
        temperature=TEMPERATURE,
    )
    if reply.load_seconds >= RELOAD_WARNING_SECONDS:
        log.warning(
            "Ollama spent %.1fs loading %s for %r: Vigie may have caused a reload of the"
            " shared model",
            reply.load_seconds,
            model,
            item.title,
        )
    try:
        output = AssessmentOutput.model_validate_json(
            reply.text, context={"topics": prompt.topic_ids}
        )
    except ValidationError:
        report.invalid += 1
        return
    store.save_assessment(
        Assessment(
            item_id=item.id,
            score=output.score,
            summary_fr=output.summary_fr,
            audience=tuple(output.audience),
            topic=output.topic,
            reason=output.reason,
            model=model,
            prompt_hash=prompt.hash,
            assessed_at=world.now(),
        )
    )
    report.assessed += 1
