"""Thin CLI: each command loads the config and calls one application entrypoint."""

import os
import re
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from vigie.assess import ScoreReport, collect_and_score, render_score_summary, score
from vigie.check_sources import check_sources, render_table
from vigie.collect import render_summary
from vigie.config import Config, ConfigError, Env, load_config, load_env
from vigie.digest import build_digest, render_digest_summary
from vigie.send import render_send_summary, send_digest
from vigie.store import open_store
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


@app.command("collect")
def collect_command(ctx: typer.Context) -> None:
    """Store new Items from every Source, with their text, then assess them."""
    config, env = _load(ctx)
    store = open_store(env.db_path)
    try:
        collected, scored = collect_and_score(
            config, store, RealWorld(ollama_host=env.ollama_host), env.llm_model
        )
    finally:
        store.close()
    typer.echo(render_summary(collected))
    _echo_score_summary(scored)


@app.command("score")
def score_command(
    ctx: typer.Context,
    rescore: Annotated[
        bool,
        typer.Option(
            "--rescore", help="Assess again every Item in the Window. Fetches nothing."
        ),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(help="With --rescore, a Window other than window_days, e.g. 14d."),
    ] = None,
) -> None:
    """Assess the Items that have text and no Assessment yet."""
    window = parse_duration(since) if since is not None else None
    if window is not None and not rescore:
        raise typer.BadParameter("only applies with --rescore", param_hint="'--since'")
    config, env = _load(ctx)
    store = open_store(env.db_path)
    try:
        report = score(
            config,
            store,
            RealWorld(ollama_host=env.ollama_host),
            env.llm_model,
            rescore=rescore,
            since=window,
        )
    finally:
        store.close()
    _echo_score_summary(report)


class Channel(StrEnum):
    slack = "slack"


@app.command("digest")
def digest_command(
    ctx: typer.Context,
    send: Annotated[
        Channel | None,
        typer.Option(help="Post this week's stored Digest, as reviewed. Builds nothing."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="With --send, send a Digest already sent.")
    ] = False,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Build this week's Digest again.")
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(help="A Window other than window_days, e.g. 14d. Implies --rebuild."),
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Folder the Markdown file is written to.")
    ] = Path("digests"),
) -> None:
    """Build this week's Digest, store it and write it as Markdown for review."""
    window = parse_duration(since) if since is not None else None
    if force and send is None:
        raise typer.BadParameter("only applies with --send", param_hint="'--force'")
    if send is not None and (rebuild or window is not None):
        raise typer.BadParameter(
            "--send posts the stored Digest: build it with --rebuild or --since first,"
            " review it, then send it",
            param_hint="'--send'",
        )
    config, env = _load(ctx)
    if send is not None:
        _send(env, force)
        return
    store = open_store(env.db_path)
    try:
        report = build_digest(
            config,
            store,
            RealWorld(ollama_host=env.ollama_host),
            env.llm_model,
            out,
            rebuild=rebuild,
            since=window,
        )
    finally:
        store.close()
    typer.echo(render_digest_summary(report))
    if report.refused:
        raise typer.Exit(1)


def _send(env: Env, force: bool) -> None:
    """`vigie digest --send slack`."""
    store = open_store(env.db_path)
    try:
        report = send_digest(store, RealWorld(slack_webhook_url=env.slack_webhook_url), force=force)
    finally:
        store.close()
    typer.echo(render_send_summary(report))
    if not report.sent:
        raise typer.Exit(1)


def _echo_score_summary(report: ScoreReport) -> None:
    typer.echo(render_score_summary(report))
    if report.error:
        raise typer.Exit(1)


DURATION_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def parse_duration(value: str) -> timedelta:
    """`14d`, `2w` or `36h`."""
    match = re.fullmatch(r"(\d+)([hdw])", value.strip())
    if not match or int(match[1]) == 0:
        raise typer.BadParameter(
            f"{value!r} is not a duration such as 14d, 2w or 36h", param_hint="'--since'"
        )
    return timedelta(**{DURATION_UNITS[match[2]]: int(match[1])})
