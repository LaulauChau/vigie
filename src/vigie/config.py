"""Config: the YAML file (settings, Topics, Sources) plus environment variables."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

YOUTUBE_PLAYLIST_FEED = "https://www.youtube.com/feeds/videos.xml?playlist_id={}"


class ConfigError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Settings(_Model):
    window_days: int = Field(gt=0)
    min_score: int = Field(ge=0, le=10)
    max_items: int = Field(gt=0)
    max_per_topic: int = Field(gt=0)
    shortlist_size: int = Field(gt=0)
    max_age_days: int = Field(gt=0)


class Topic(_Model):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class RssSource(_Model):
    type: Literal["rss"]
    name: str = Field(min_length=1)
    url: str


class YoutubeSource(_Model):
    type: Literal["youtube"]
    name: str = Field(min_length=1)
    # The channel's long-form-only playlist (UULF...), which leaves Shorts out.
    playlist_id: str = Field(min_length=1)

    @property
    def url(self) -> str:
        return YOUTUBE_PLAYLIST_FEED.format(self.playlist_id)


class HtmlIndexSource(_Model):
    type: Literal["html_index"]
    name: str = Field(min_length=1)
    url: str
    # Searched against the path of each link found on the page, same host only.
    link_regex: str
    exclude: list[str] = []

    @field_validator("link_regex")
    @classmethod
    def _regex_compiles(cls, v: str) -> str:
        return _check_regex(v)

    @field_validator("exclude")
    @classmethod
    def _regexes_compile(cls, v: list[str]) -> list[str]:
        return [_check_regex(p) for p in v]


def _check_regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid regex {pattern!r}: {e}") from e
    return pattern


Source = Annotated[RssSource | YoutubeSource | HtmlIndexSource, Field(discriminator="type")]


class Config(_Model):
    settings: Settings
    topics: list[Topic] = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> "Config":
        _check_unique("Topic id", [t.id for t in self.topics])
        _check_unique("Source name", [s.name for s in self.sources])
        return self


def _check_unique(what: str, values: list[str]) -> None:
    seen = set()
    for v in values:
        if v in seen:
            raise ValueError(f"duplicate {what} {v!r}")
        seen.add(v)


class Env(_Model):
    ollama_host: str | None
    llm_model: str
    slack_webhook_url: str | None
    db_path: Path


def load_env(environ: Mapping[str, str]) -> Env:
    def get(name: str) -> str | None:
        return environ.get(name) or None

    return Env(
        ollama_host=get("OLLAMA_HOST"),
        llm_model=get("LLM_MODEL") or "qwen3.8",
        slack_webhook_url=get("SLACK_WEBHOOK_URL"),
        db_path=Path(get("DB_PATH") or "vigie.db"),
    )


def load_config(path: Path) -> Config:
    """Read and validate the config file, or raise ConfigError with a clear message."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {path}:\n{_describe(e, raw)}") from e


def _describe(error: ValidationError, raw: object) -> str:
    lines = []
    for err in error.errors():
        loc = list(err["loc"])
        # Discriminated unions add the tag to the location (sources.3.rss.url); drop it.
        if len(loc) >= 3 and loc[0] == "sources" and loc[2] in ("rss", "youtube", "html_index"):
            del loc[2]
        where = ".".join(str(p) for p in loc) or "(top level)"
        lines.append(f"  - {where}{_source_label(loc, raw)}: {_message(err)}")
    return "\n".join(lines)


def _source_label(loc: list[int | str], raw: object) -> str:
    """Name the Source an error is about, so the operator can find it in the file."""
    if len(loc) >= 2 and loc[0] == "sources" and isinstance(loc[1], int) and isinstance(raw, dict):
        sources = raw.get("sources")
        if isinstance(sources, list) and loc[1] < len(sources):
            entry = sources[loc[1]]
            if isinstance(entry, dict) and entry.get("name"):
                return f" ({entry['name']})"
    return ""


def _message(err: Mapping[str, object]) -> str:
    if err["type"] == "union_tag_invalid":
        ctx = err.get("ctx")
        tag = ctx.get("tag") if isinstance(ctx, dict) else "?"
        return f"unknown Source type {tag!r}, expected one of: rss, youtube, html_index"
    if err["type"] == "union_tag_not_found":
        return "missing Source type, expected one of: rss, youtube, html_index"
    return str(err["msg"])
