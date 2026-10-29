from pathlib import Path

from typer.testing import CliRunner

from vigie.cli import app

runner = CliRunner()


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "check-sources" in result.output
    assert "collect" in result.output


def test_invalid_config_exits_with_its_message(tmp_path: Path) -> None:
    path = tmp_path / "vigie.yaml"
    path.write_text("settings: {}\ntopics: []\nsources: []\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(path), "check-sources"])

    assert result.exit_code == 1
    assert f"invalid config in {path}" in result.output
