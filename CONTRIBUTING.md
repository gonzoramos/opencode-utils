# Contributing

Bug reports, feature ideas, and pull requests are all welcome —
[open an issue](https://github.com/gonzoramos/opencode-utils/issues) to start a
discussion.

## Adding a new tool

Each tool is a self-contained package under `packages/`, exposed as a console command.

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
automatically. Run `uv sync` and the new `<name>` command is available via `uv run`:

```bash
uv run <name>
```

Please include a `README.md` in your package describing what the tool does, how to run
it, and any provider/config assumptions it makes.
