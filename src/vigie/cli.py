"""Thin CLI: each command loads the config and calls one application entrypoint."""

import os
from pathlib import Path
from typing import Annotated

import typer

from vigie.check_sources import check_sources, render_table
from vigie.config import Config, ConfigError, Env, load_config, load_env
from vigie.world import RealWorld

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Path to the YAML config file.")
    ] = Path("vigie.yaml"),
) -> None:
    """Weekly Slack digest of AI-practice picks."""
    ctx.obj = config


def _load(ctx: typer.Context) -> tuple[Config, Env]:
    try:
        return load_config(ctx.obj), load_env(os.environ)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@app.command("check-sources")
def check_sources_command(ctx: typer.Context) -> None:
    """Fetch every Source once and print its status. Writes nothing."""
    config, _ = _load(ctx)
    typer.echo(render_table(check_sources(config, RealWorld())))
