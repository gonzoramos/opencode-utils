# opencode-utils

A monorepo of small Python utilities for managing an
[OpenCode](https://opencode.ai) setup — particularly one that uses local LLM servers
(oMLX and Ollama) as providers.

Managed as a [uv](https://docs.astral.sh/uv/) workspace: each utility is a self-contained
package under `packages/`, sharing a single lockfile and virtual environment, and is
exposed as a console command.

## Tools

| Command        | Package                | What it does                                                          |
|----------------|------------------------|----------------------------------------------------------------------|
| `sync-models`  | `packages/sync-models` | Sync `opencode.json` provider models with local oMLX/Ollama servers. |

## Quickstart

```bash
# Resolve dependencies and create the shared .venv
uv sync

# Run any tool by its command name
uv run sync-models --dry-run
```

## Adding a new tool

```bash
# Scaffold a new packaged member
uv init --package packages/<name>
```

Then in `packages/<name>/pyproject.toml`, add a console entry point:

```toml
[project.scripts]
<name> = "<import_pkg>.cli:main"
```

The workspace root's `[tool.uv.workspace] members = ["packages/*"]` picks it up
automatically. Run `uv sync` and the new `<name>` command is available via `uv run`.

## Layout

```
opencode-utils/
├── pyproject.toml          # workspace root (non-package)
├── uv.lock                 # shared lockfile
└── packages/
    └── sync-models/        # one tool = one package (src/ layout, console script)
```
