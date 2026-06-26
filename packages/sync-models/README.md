# sync-models

Syncs the `provider.*.models` sections of your OpenCode config
(`~/.config/opencode/opencode.json`) with whatever models are currently available
on your local oMLX and Ollama servers.

It reads the provider endpoints from your existing config, queries each one for its
available models, and updates the config file — all within a **Textual TUI** that
shows live per-provider probing status and per-model progress while baking `num_ctx`.

No interaction required — runs are fully automatic. Press any key to exit when done.

## Usage

From the monorepo root:

```bash
# Preview changes without writing
uv run sync-models --dry-run

# Run (auto-applies any changes)
uv run sync-models

# Point at a different config
uv run sync-models --config /path/to/opencode.json

# Also bake num_ctx into Ollama models so they stop truncating to 2048
uv run sync-models --set-num-ctx                      # half each model's max (default)
uv run sync-models --set-num-ctx --ctx-fraction 0.25  # quarter of the max instead
uv run sync-models --set-num-ctx --max-ctx 131072     # half, but never above 131072
uv run sync-models --dry-run --set-num-ctx            # preview the num_ctx plan
```

## TUI feedback

The Textual full-screen TUI replaces the original ANSI-escape CLI output with a
structured live view:

1. **Probing** — each provider row starts as `⏳ probing...` and updates to
   `✓ N models` or `✗ unreachable` as results arrive.
2. **Diff** — pending additions (`+`, green), removals (`-`, red), and context
   changes (`~`, yellow) are printed per provider.
3. **Apply** — if there are changes (and `--dry-run` was not passed), the tool
   auto-advances to apply them. A progress bar shows overall completion while
   each model being baked shows `⏳ baking num_ctx...` → `✓ done` / `✗ error`.
4. **Exit** — once finished (or on `--dry-run`), `Press any key to exit...` is
   shown and the TUI waits for a keypress before closing.

## Ollama context windows (`num_ctx`)

Ollama defaults to a **2048-token** context at inference time regardless of a
model's architectural max — and its OpenAI-compatible `/v1/chat/completions`
endpoint (which OpenCode uses) **cannot** override this per request. So a model
advertising a 256k context still silently truncates to 2048 unless `num_ctx` is
baked into the model.

Because of this:

- For Ollama models, `limit.context` reflects the **effective** baked `num_ctx`
  (read from `/api/show` `parameters`, defaulting to `2048`) — **not** the
  architectural max. This keeps OpenCode from sending more than Ollama honours.
- `--set-num-ctx` re-creates each Ollama model **in place** (`POST /api/create`
  with `from == model`, inheriting weights/template/other params) with a baked
  `num_ctx`, then updates `limit.context` to match.
  - **Target** = `--ctx-fraction` × the model's architectural max (default
    `0.5`, i.e. half). Architectural maxes are often 128k–500k; using the full
    max would allocate a huge KV cache, while a small fixed window is too tight
    for coding agents — half is a sensible middle ground.
  - **`--max-ctx`** (optional) caps the target at an absolute value. By default
    there is no ceiling and the fraction governs.
  - `num_ctx` RAM cost scales roughly linearly, so lower `--ctx-fraction` (or set
    `--max-ctx`) if a model fails to load or you're tight on memory.

## Local server details

| Provider key | URL                          | Discovery API                          |
|--------------|------------------------------|----------------------------------------|
| `omlx`       | `http://127.0.0.1:8067/v1`   | `GET /models` → `data[].max_model_len` |
| `ollama`     | `http://localhost:11434/v1`  | `GET /v1/models` + `POST /api/show`    |

oMLX is detected when the base URL does **not** contain port `11434` and the provider
key is not `"ollama"`. Ollama is detected when the base URL contains `11434` or the
provider key contains `"ollama"`.

## opencode.json provider entry shape

```json
{
  "npm": "@ai-sdk/openai-compatible",
  "name": "Human-readable label",
  "options": { "baseURL": "...", "apiKey": "..." },
  "models": {
    "<model-id>": {
      "name": "Friendly name",
      "modalities": { "input": ["text"], "output": ["text"] },
      "attachment": false,
      "limit": { "context": 131072, "output": 8192 }
    }
  }
}
```

## Conventions

- The TUI uses [Textual](https://textual.textualize.io/); all widget mutations
  are dispatched from worker threads via `call_from_thread` to stay safe.
- Vision/multimodal detection for oMLX uses name heuristics (`_is_vision_by_name`);
  for Ollama it uses the `capabilities` array from `/api/show`.
- When a model already exists in config, only `limit.context` is updated on sync —
  friendly names and modalities set by the user are preserved.
- Output limits: oMLX models default to `32768`, Ollama models default to `8192`.
