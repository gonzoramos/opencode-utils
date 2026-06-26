#!/usr/bin/env python3
"""
sync_models.py — Keep opencode.json in sync with local oMLX and Ollama models.

Usage:
    sync-models [--config PATH] [--dry-run] [--set-num-ctx] [--ctx-fraction F] [--max-ctx N]
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

from textual.app import App
from textual.containers import Container
from textual.widgets import Header, Footer, Static, RichLog, ProgressBar
from textual import work

DEFAULT_CONFIG = Path.home() / ".config/opencode/opencode.json"

OLLAMA_DEFAULT_NUM_CTX = 2048
DEFAULT_CTX_FRACTION = 0.5

# ── HTTP helpers ──────────────────────────────────────────────────────────────


def fetch(url, method="GET", body=None, timeout=5):
    try:
        req = urllib.request.Request(url, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── Model discovery ───────────────────────────────────────────────────────────


def _pretty_name(model_id: str) -> str:
    return model_id.replace("-", " ").replace("_", " ").replace(":", " ").title()


def _is_vision_by_name(name: str) -> bool:
    tokens = name.lower()
    return any(x in tokens for x in [
        "gemma-4", "gemma4", "llava", "vision", "vlm",
        "pixtral", "qwen-vl", "minicpm-v", "north-mini",
    ])


def discover_omlx(base_url: str) -> dict | None:
    data = fetch(f"{base_url}/models")
    if data is None:
        return None

    models = {}
    for m in data.get("data", []):
        mid = m["id"]
        ctx = m.get("max_model_len", 32768)
        vision = _is_vision_by_name(mid)
        models[mid] = {
            "name": _pretty_name(mid),
            "modalities": {
                "input": ["text", "image"] if vision else ["text"],
                "output": ["text"],
            },
            "attachment": vision,
            "limit": {
                "context": ctx,
                "output": 32768,
            },
        }
    return models


def _parse_num_ctx(parameters: str | None) -> int | None:
    """Extract the baked num_ctx from /api/show's `parameters` text, else None.

    The field is a whitespace-formatted block, e.g.::

        temperature                    1
        num_ctx                        32768
    """
    for line in (parameters or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def _ollama_arch_max(details: dict) -> int | None:
    model_info = details.get("model_info", {})
    ctx_key = next((k for k in model_info if k.endswith(".context_length")), None)
    return model_info[ctx_key] if ctx_key else None


def discover_ollama(base_url: str) -> dict | None:
    """Return {model_id: entry_dict} from Ollama.

    `limit.context` reflects the *effective* context window — the baked num_ctx
    that Ollama will actually honour at inference time — not the architectural
    max. Without a baked num_ctx, Ollama silently truncates to 2048 regardless
    of how large the model's context_length is, so reporting the arch max here
    would tell OpenCode it can send far more than Ollama will accept. Use
    --set-num-ctx to raise the baked num_ctx (and therefore this value).
    """
    data = fetch(f"{base_url}/v1/models")
    if data is None:
        return None

    models = {}
    for m in data.get("data", []):
        mid = m["id"]
        details = fetch(f"{base_url}/api/show", method="POST", body={"name": mid})
        if not details:
            continue

        ctx = _parse_num_ctx(details.get("parameters")) or OLLAMA_DEFAULT_NUM_CTX

        vision = "vision" in details.get("capabilities", [])

        models[mid] = {
            "name": _pretty_name(mid),
            "modalities": {
                "input": ["text", "image"] if vision else ["text"],
                "output": ["text"],
            },
            "attachment": vision,
            "limit": {
                "context": ctx,
                "output": 8192,
            },
        }
    return models


def _target_num_ctx(arch_max: int, fraction: float, max_ctx: int | None) -> int:
    """num_ctx to bake: `fraction` of the arch max, optionally capped by max_ctx.

    Always at least 1024 and never above the arch max itself.
    """
    target = int(arch_max * fraction)
    if max_ctx:
        target = min(target, max_ctx)
    return max(1024, min(target, arch_max))


def ollama_ctx_plan(root_url: str, fraction: float,
                    max_ctx: int | None = None) -> dict | None:
    """Plan num_ctx changes for every Ollama model.

    Returns {model_id: (current, target, arch_max)} for models whose effective
    num_ctx differs from the target, where target is `fraction` of the model's
    architectural max (optionally capped by max_ctx). None if unreachable.
    """
    data = fetch(f"{root_url}/v1/models")
    if data is None:
        return None

    plan = {}
    for m in data.get("data", []):
        mid = m["id"]
        details = fetch(f"{root_url}/api/show", method="POST", body={"name": mid})
        if not details:
            continue

        arch_max = _ollama_arch_max(details)
        if arch_max is None:
            continue

        current = _parse_num_ctx(details.get("parameters")) or OLLAMA_DEFAULT_NUM_CTX
        target = _target_num_ctx(arch_max, fraction, max_ctx)
        if current != target:
            plan[mid] = (current, target, arch_max)
    return plan


def set_num_ctx(root_url: str, model: str, num_ctx: int) -> tuple[bool, str]:
    """Re-create `model` in place with num_ctx baked in, via POST /api/create.

    `from == model` inherits the existing weights, template, and other baked
    parameters; only num_ctx is added/overridden. Returns (ok, status_message).
    """
    body = {
        "model": model,
        "from": model,
        "parameters": {"num_ctx": num_ctx},
        "stream": False,
    }
    try:
        req = urllib.request.Request(f"{root_url}/api/create", method="POST")
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read().decode(errors="replace").strip()
        try:
            resp = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            resp = {}
        if "error" in resp:
            return (False, str(resp["error"]))
        return (True, resp.get("status", "success"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        return (False, f"HTTP {e.code}: {detail}")
    except Exception as e:
        return (False, str(e))


def is_ollama(provider_key: str, base_url: str) -> bool:
    """Ollama is detected by its default port or an "ollama" provider key."""
    return "11434" in base_url or "ollama" in provider_key.lower()


def ollama_root(base_url: str) -> str:
    """Root URL for Ollama's native API (drops the OpenAI-compat /v1 suffix)."""
    return base_url.rstrip("/").replace("/v1", "")


def probe_provider(provider_key: str, options: dict) -> dict | None:
    """
    Pick the right discovery function based on provider key / base URL.
    Returns None if the server is unreachable.
    """
    base_url = options.get("baseURL", "").rstrip("/")
    if not base_url:
        return None

    if is_ollama(provider_key, base_url):
        return discover_ollama(ollama_root(base_url))
    else:
        return discover_omlx(base_url)


# ── Diffing ───────────────────────────────────────────────────────────────────


def diff_provider(config_models: dict, live_models: dict):
    """
    Returns:
        to_add    – set of IDs present in live but not config
        to_remove – set of IDs present in config but not live
        to_update – {id: {field: (old_val, new_val)}}  for context changes
    """
    cfg_keys  = set(config_models)
    live_keys = set(live_models)

    to_add    = live_keys - cfg_keys
    to_remove = cfg_keys  - live_keys
    to_update = {}

    for mid in cfg_keys & live_keys:
        changes = {}
        old_ctx = config_models[mid].get("limit", {}).get("context")
        new_ctx = live_models[mid]["limit"]["context"]
        if old_ctx != new_ctx:
            changes["context"] = (old_ctx, new_ctx)
        if changes:
            to_update[mid] = changes

    return to_add, to_remove, to_update


# ── Applying changes ──────────────────────────────────────────────────────────


def apply(config_models: dict, live_models: dict,
          to_add, to_remove, to_update) -> dict:
    result = dict(config_models)

    for mid in to_remove:
        del result[mid]

    for mid in to_add:
        result[mid] = dict(live_models[mid])

    for mid, changes in to_update.items():
        if "context" in changes:
            result[mid].setdefault("limit", {})["context"] = changes["context"][1]

    return result


# ── Textual TUI ──────────────────────────────────────────────────────────────


class SyncApp(App):
    """Textual TUI for syncing opencode.json with running oMLX / Ollama models."""

    TITLE = "sync-models"

    CSS = """
    #status-title {
        margin: 0 1;
        padding: 1 2 0 2;
    }
    #provider-area {
        height: auto;
        max-height: 12;
        margin: 0 2;
    }
    #diff-area {
        height: auto;
        max-height: 20;
        margin: 0 1;
        padding: 0 1;
    }
    #progress-area {
        height: auto;
        margin: 0 2;
    }
    #overall-progress {
        height: 1;
        margin: 1 0;
    }
    """

    def __init__(self, config: dict, config_path: Path, args):
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.args = args
        self.pending: dict = {}
        self.ctx_pending: dict = {}
        self._providers: list[dict] = []
        self._model_widgets: dict[str, Static] = {}
        self._exit_prompt: bool = False

    def compose(self):
        yield Header()
        yield Static("[bold]Checking providers...[/bold]", id="status-title")
        yield Container(id="provider-area")
        yield RichLog(id="diff-area", highlight=True, markup=True)
        yield Container(id="progress-area")
        yield Footer()

    def on_mount(self):
        self.probe_all()

    # ── Probing ──────────────────────────────────────────────────────────────

    @work(thread=True, exit_on_error=False)
    def probe_all(self):
        providers = self.config.get("provider", {})

        for pkey, pcfg in providers.items():
            options = pcfg.get("options", {})
            base_url = options.get("baseURL", "")

            self.call_from_thread(self._add_provider_row, pkey, base_url)

            live = probe_provider(pkey, options)
            if live is None:
                self.call_from_thread(self._set_provider_status, pkey, False,
                                      "[red]✗[/red] unreachable")
                continue

            self.call_from_thread(self._set_provider_status, pkey, True,
                                  f"[green]✓[/green] {len(live)} models")

            config_models = pcfg.get("models", {})
            to_add, to_remove, to_update = diff_provider(config_models, live)

            if self.args.set_num_ctx and is_ollama(pkey, base_url.rstrip("/")):
                root = ollama_root(base_url)
                plan = ollama_ctx_plan(root, self.args.ctx_fraction, self.args.max_ctx)
                if plan:
                    for mid in plan:
                        to_update.pop(mid, None)
                    self.ctx_pending[pkey] = (plan, root)

            if to_add or to_remove or to_update:
                self.pending[pkey] = (to_add, to_remove, to_update, live)

        self.call_from_thread(self._probing_done)

    def _add_provider_row(self, pkey: str, base_url: str):
        row = Static(
            f"  [yellow]⏳[/yellow] [bold]{pkey}[/bold] "
            f"[dim]({base_url})[/dim]  [yellow]probing...[/yellow]"
        )
        self.query_one("#provider-area").mount(row)
        self._providers.append({"pkey": pkey, "widget": row, "base_url": base_url})

    def _set_provider_status(self, pkey: str, ok: bool, detail: str):
        for prov in self._providers:
            if prov["pkey"] == pkey:
                color = "green" if ok else "red"
                icon = "✓" if ok else "✗"
                prov["widget"].update(
                    f"  [{color}]{icon}[/{color}] [bold]{pkey}[/bold] "
                    f"[dim]({prov['base_url']})[/dim]  {detail}"
                )
                break

    def _probing_done(self):
        self.query_one("#status-title").update("[bold]Providers[/bold]")
        diff = self.query_one("#diff-area")

        if not self.pending and not self.ctx_pending:
            diff.write("[green]✓ Already up to date — nothing to change.[/green]")
            self._show_exit_prompt()
            return

        diff.write("[bold]Proposed changes:[/bold]")
        for pkey, (to_add, to_remove, to_update, _) in self.pending.items():
            diff.write(f"\n  [bold cyan]{pkey}[/bold cyan]")
            for mid in sorted(to_add):
                diff.write(f"    [green]+ {mid}[/green]")
            for mid in sorted(to_remove):
                diff.write(f"    [red]- {mid}[/red]")
            for mid, changes in sorted(to_update.items()):
                for field, (old, new) in changes.items():
                    diff.write(
                        f"    [yellow]~ {mid}  {field}: {old} → {new}[/yellow]"
                    )
        for pkey, (plan, _) in self.ctx_pending.items():
            diff.write(f"\n  [bold cyan]{pkey}[/bold cyan] [dim](num_ctx)[/dim]")
            for mid, (cur, tgt, arch) in sorted(plan.items()):
                diff.write(
                    f"    [yellow]~ {mid}  num_ctx: {cur} → {tgt}[/yellow] "
                    f"[dim](model max {arch})[/dim]"
                )

        if self.args.dry_run:
            diff.write("\n[dim](dry-run — config not modified)[/dim]")
            self._show_exit_prompt()
            return

        self.do_apply()

    # ── Applying ─────────────────────────────────────────────────────────────

    @work(thread=True, exit_on_error=False)
    def do_apply(self):
        self.call_from_thread(self._setup_progress)

        for pkey, (to_add, to_remove, to_update, live) in self.pending.items():
            self.config["provider"][pkey]["models"] = apply(
                self.config["provider"][pkey].get("models", {}),
                live, to_add, to_remove, to_update,
            )

        total = sum(len(plan) for plan, _ in self.ctx_pending.values())
        done = 0

        for pkey, (plan, root) in self.ctx_pending.items():
            models_cfg = self.config["provider"][pkey].setdefault("models", {})
            for mid, (cur, tgt, arch) in sorted(plan.items()):
                self.call_from_thread(self._set_model_status, mid,
                                      "[yellow]⏳[/yellow] baking num_ctx...")
                ok, msg = set_num_ctx(root, mid, tgt)
                effective = tgt if ok else cur
                done += 1
                label = "[green]✓[/green] done" if ok else f"[red]✗[/red] {msg}"
                self.call_from_thread(self._set_model_status, mid, label)
                self.call_from_thread(self._update_progress_bar, done, total)
                if mid in models_cfg:
                    models_cfg[mid].setdefault("limit", {})["context"] = effective

        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)
            f.write("\n")

        self.call_from_thread(self._apply_done)

    def _setup_progress(self):
        area = self.query_one("#progress-area")
        area.mount(
            Static("[bold]Applying changes:[/bold]", id="progress-title"),
            ProgressBar(id="overall-progress", total=100, show_eta=False),
        )
        for pkey, (plan, _) in self.ctx_pending.items():
            for mid, _ in sorted(plan.items()):
                safe = mid.replace(":", "-").replace("/", "-")
                w = Static(f"  [dim]{mid}[/dim]  [yellow]⏳[/yellow] pending",
                           id=f"m-{safe}")
                self._model_widgets[mid] = w
                area.mount(w)

    def _set_model_status(self, mid: str, status: str):
        w = self._model_widgets.get(mid)
        if w:
            w.update(f"  {mid}  {status}")

    def _update_progress_bar(self, done: int, total: int):
        bar = self.query_one("#overall-progress")
        bar.progress = int(done / total * 100) if total > 0 else 100

    def _apply_done(self):
        self.query_one("#diff-area").write("\n[green]✓ Config updated.[/green]")
        bar = self.query_one("#overall-progress")
        bar.progress = 100
        bar.display = False
        self._show_exit_prompt()

    def _show_exit_prompt(self):
        self.query_one("#diff-area").write("[dim]Press any key to exit...[/dim]")
        self._exit_prompt = True

    def on_key(self, event):
        if self._exit_prompt:
            self.exit()


# ── Entry point ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Sync opencode.json with running oMLX / Ollama models."
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG),
        help=f"Path to opencode.json (default: {DEFAULT_CONFIG})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing anything"
    )
    parser.add_argument(
        "--set-num-ctx", action="store_true",
        help="Bake num_ctx into Ollama models so they stop truncating to the "
             "2048 default, and align limit.context. Target defaults to half "
             "the model's reported max (see --ctx-fraction / --max-ctx)"
    )
    parser.add_argument(
        "--ctx-fraction", type=float, default=DEFAULT_CTX_FRACTION,
        help=f"Fraction of a model's architectural max to bake as num_ctx with "
             f"--set-num-ctx (default: {DEFAULT_CTX_FRACTION})"
    )
    parser.add_argument(
        "--max-ctx", type=int, default=None,
        help="Optional absolute ceiling for Ollama num_ctx with --set-num-ctx "
             "(default: no ceiling — the fraction governs)"
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    if not config.get("provider"):
        print("No providers found in config.")
        sys.exit(0)

    app = SyncApp(config, config_path, args)
    app.run()


if __name__ == "__main__":
    main()
