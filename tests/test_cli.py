from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vigie.cli import app, parse_duration

runner = CliRunner()


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "check-sources" in result.output
    assert "collect" in result.output
    assert "score" in result.output
    assert "digest" in result.output


@pytest.mark.parametrize(
    ("value", "expected"),
    [("14d", timedelta(days=14)), ("2w", timedelta(weeks=2)), ("36h", timedelta(hours=36))],
)
def test_since_takes_a_duration(value: str, expected: timedelta) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["14", "two weeks", "0d", "-3d"])
def test_score_rejects_a_since_that_is_not_a_duration(value: str) -> None:
    result = runner.invoke(app, ["score", "--rescore", "--since", value])

    assert result.exit_code == 2
    assert "is not a duration such as 14d" in result.output


def test_score_rejects_since_without_rescore() -> None:
    result = runner.invoke(app, ["score", "--since", "14d"])

    assert result.exit_code == 2
    assert "only applies with --rescore" in result.output


def test_invalid_config_exits_with_its_message(tmp_path: Path) -> None:
    path = tmp_path / "vigie.yaml"
    path.write_text("settings: {}\ntopics: []\nsources: []\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(path), "check-sources"])

    assert result.exit_code == 1
    assert f"invalid config in {path}" in result.output


def test_digest_rejects_a_since_that_is_not_a_duration() -> None:
    result = runner.invoke(app, ["digest", "--since", "two weeks"])

    assert result.exit_code == 2
    assert "is not a duration such as 14d" in result.output


def test_digest_with_nothing_collected_explains_the_empty_digest(tmp_path: Path) -> None:
    out = tmp_path / "digests"

    result = runner.invoke(
        app, ["digest", "--out", str(out)], env={"DB_PATH": str(tmp_path / "vigie.db")}
    )

    assert result.exit_code == 0
    assert "nothing stored or written" in result.output
    assert "Items collected in the Window: 0" in result.output
    assert not out.exists()
