# opencode-utils

> A monorepo of small Python utilities for managing an [OpenCode](https://opencode.ai)
> setup — particularly one that uses **local LLM servers** ([oMLX](https://github.com/ml-explore/mlx)
> and [Ollama](https://ollama.com)) as providers.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Managed with uv](https://img.shields.io/badge/managed%20with-uv-purple)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

The repo is managed as a [uv](https://docs.astral.sh/uv/) workspace: each utility is a
self-contained package under `packages/`, sharing a single lockfile and virtual
environment, and is exposed as a console command.

## Tools

| Command | Package | What it does |
|---|---|---|
| [`sync-models`](packages/sync-models/) | `packages/sync-models` | Sync your `opencode.json` provider models with whatever is running on your local oMLX / Ollama servers — with a live Textual TUI. |

> More tools will land here over time. Have an idea? [Open an issue](https://github.com/gonzoramos/opencode-utils/issues).

## Requirements

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — `brew install uv` or `pip install uv`
- **[OpenCode](https://opencode.ai)** with at least one local provider (oMLX or Ollama)
  configured in `~/.config/opencode/opencode.json`

## Quickstart

```bash
git clone https://github.com/gonzoramos/opencode-utils
cd opencode-utils

# Resolve dependencies and create the shared .venv
uv sync

# Run any tool by its command name
uv run sync-models --dry-run
```

See each tool's README for full usage — start with [`sync-models`](packages/sync-models/).

## Layout

```
opencode-utils/
├── pyproject.toml          # workspace root (non-package)
├── uv.lock                 # shared lockfile
└── packages/
    └── sync-models/        # one tool = one package (src/ layout, console script)
```

## Contributing

Bug reports and feature ideas are welcome — [open an issue](https://github.com/gonzoramos/opencode-utils/issues).

To add a new tool to the workspace, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Gonzalo Ramos
