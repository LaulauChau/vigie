from pathlib import Path

import pytest

from vigie.config import ConfigError, load_config, load_env

REPO_CONFIG = Path(__file__).parent.parent / "vigie.yaml"

VALID_SETTINGS = """
settings:
  window_days: 7
  min_score: 6
  max_items: 5
  max_per_topic: 3
  shortlist_size: 10
  max_age_days: 30
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "vigie.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_config_holds_the_spec_settings_topics_and_sources() -> None:
    config = load_config(REPO_CONFIG)

    assert config.settings.model_dump() == {
        "window_days": 7,
        "min_score": 6,
        "max_items": 5,
        "max_per_topic": 3,
        "shortlist_size": 10,
        "max_age_days": 30,
    }
    assert [t.id for t in config.topics] == [
        "pratique-dev-data",
        "cas-usage-entreprise",
        "risques-et-couts",
    ]
    by_type: dict[str, int] = {}
    for source in config.sources:
        by_type[source.type] = by_type.get(source.type, 0) + 1
    # 10 blogs and newsletters plus 4 HN queries, 4 YouTube channels, 2 HTML indexes.
    assert by_type == {"rss": 14, "youtube": 4, "html_index": 2}
    urls = {s.url for s in config.sources}
    assert "https://simonwillison.net/atom/entries/" in urls
    assert "https://www.youtube.com/feeds/videos.xml?playlist_id=UULFswG6FSbgZjbWtdf_hMLaow" in urls


def test_unknown_source_type_fails_with_a_clear_message(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        VALID_SETTINGS
        + """
topics:
  - id: pratique-dev-data
    description: Pratique.
sources:
  - name: Some blog
    type: atom
    url: https://blog.example.com/feed
""",
    )

    with pytest.raises(ConfigError) as error:
        load_config(path)

    assert "sources.0 (Some blog): unknown Source type 'atom'" in str(error.value)


def test_topic_without_id_fails_with_a_clear_message(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        VALID_SETTINGS
        + """
topics:
  - description: Pratique.
sources:
  - name: Some blog
    type: rss
    url: https://blog.example.com/feed
""",
    )

    with pytest.raises(ConfigError) as error:
        load_config(path)

    assert "topics.0.id: Field required" in str(error.value)


def test_env_defaults_the_model_and_reads_the_other_variables() -> None:
    env = load_env({"OLLAMA_HOST": "http://ollama.internal:11434", "DB_PATH": "/tmp/v.db"})

    assert env.llm_model == "qwen3.8"
    assert env.ollama_host == "http://ollama.internal:11434"
    assert env.db_path == Path("/tmp/v.db")
    assert env.slack_webhook_url is None
