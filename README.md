# Vigie

Weekly Slack digest of up to five picks on AI practice in dev and data, scored by a local Ollama LLM. Throwaway POC, run by hand.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync
cp .env.example .env   # then fill in the values
```

Settings, Topics and Sources live in `vigie.yaml`. Pass another file with `--config`.

## Commands

```sh
uv run --env-file .env vigie --help
uv run --env-file .env vigie check-sources   # status table of every Source, writes nothing
uv run --env-file .env vigie collect         # store new Items from every Source with their text in DB_PATH, then assess them
uv run --env-file .env vigie score           # assess Items not assessed yet (collect does this too)
uv run --env-file .env vigie score --rescore [--since 14d]   # assess the Window again, fetches nothing
uv run --env-file .env vigie digest          # build this week's Digest, store it, write digests/<build date>.md
uv run --env-file .env vigie digest --rebuild [--since 14d]  # build this week's Digest again
```

`vigie digest` picks up to `max_items` Items by Score from the Window. Running it again in the same week writes the stored Digest again, unchanged. The Markdown file shows the message, then a review appendix that is never sent.

Scoring never makes the shared Ollama model reload (see `docs/adr/0001-keep-the-shared-ollama-context-size.md`). A warning is logged if a request's load time suggests it did.

## Development

```sh
uv run pytest
uv run mypy src tests
```
