# opencode-utils — Agent Instructions

## Project purpose
A monorepo of small Python utilities for managing an OpenCode
(`~/.config/opencode/opencode.json`) setup that uses local LLM servers (oMLX and
Ollama) as providers.

## Structure
Managed as a **uv workspace**. Each utility is a self-contained package under
`packages/`, sharing one lockfile (`uv.lock`) and one virtual environment, and is
exposed as a console command via `[project.scripts]`.

```
opencode-utils/
├── pyproject.toml          # workspace root (non-package): [tool.uv.workspace]
├── uv.lock                 # shared lockfile
└── packages/
    └── sync-models/        # first tool — see its README for details
        ├── pyproject.toml  # member package + console entry point
        └── src/sync_models/cli.py
```

## How to run / test
```bash
uv sync                       # resolve deps, create .venv, write uv.lock
uv run sync-models --dry-run  # run a tool by its command name
```

Per-tool documentation lives in each package's own `README.md` (e.g.
`packages/sync-models/README.md` for run flags, provider details, and config shape).

## Adding a new tool
1. `uv init --package packages/<name>` — scaffolds a packaged member.
2. Add a console entry point in `packages/<name>/pyproject.toml`:
   ```toml
   [project.scripts]
   <name> = "<import_pkg>.cli:main"
   ```
3. `uv sync` — the `members = ["packages/*"]` glob picks it up automatically.

## Conventions
- Python-only; one tool = one package under `packages/`, using a `src/` layout.
- Prefer stdlib-only tools unless there's a strong reason to add dependencies.
- Each tool exposes a `main()` entry point wired through `[project.scripts]`.
