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
```

## Development

```sh
uv run pytest
uv run mypy src tests
```
