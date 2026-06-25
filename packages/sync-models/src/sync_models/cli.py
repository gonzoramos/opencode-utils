#!/usr/bin/env python3
"""
sync_models.py — Keep opencode.json in sync with local oMLX and Ollama models.

Usage:
    python sync_models.py [--config PATH] [--dry-run]

Reads provider endpoints from your existing opencode.json, queries each one
for available models, then shows a diff of what would be added, removed, or
updated. Prompts before writing any changes.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config/opencode/opencode.json"

# Ollama's runtime context window when num_ctx is not baked into the model.
OLLAMA_DEFAULT_NUM_CTX = 2048
# Default fraction of a model's architectural max to bake as num_ctx. Using the
# full max would allocate an impractically large KV cache (maxes are often
# 256k–500k), but a small fixed ceiling is too tight for coding agents — half
# the reported max is a sensible middle ground, tunable via --ctx-fraction.
DEFAULT_CTX_FRACTION = 0.5

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


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
    """Turn a model ID into a friendlier display name."""
    return model_id.replace("-", " ").replace("_", " ").replace(":", " ").title()


def _is_vision_by_name(name: str) -> bool:
    tokens = name.lower()
    return any(x in tokens for x in [
        "gemma-4", "gemma4", "llava", "vision", "vlm",
        "pixtral", "qwen-vl", "minicpm-v", "north-mini",
    ])


def discover_omlx(base_url: str) -> dict | None:
    """Return {model_id: entry_dict} from an oMLX-compatible endpoint."""
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
    """The model's architectural max context (<arch>.context_length), or None."""
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
            continue  # can't size a target without a known max — skip

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
        # urlopen raises HTTPError on non-2xx, so reaching here means success;
        # the body is informational (stream:false returns a single object).
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
    except Exception as e:  # noqa: BLE001 — surface any failure to the caller
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


def print_provider_diff(provider_key, to_add, to_remove, to_update):
    print(f"\n  {BOLD}{CYAN}{provider_key}{RESET}")
    for mid in sorted(to_add):
        print(f"    {GREEN}+ {mid}{RESET}")
    for mid in sorted(to_remove):
        print(f"    {RED}- {mid}{RESET}")
    for mid, changes in sorted(to_update.items()):
        for field, (old, new) in changes.items():
            print(f"    {YELLOW}~ {mid}  {field}: {old} → {new}{RESET}")


def print_ctx_plan(provider_key, plan):
    print(f"\n  {BOLD}{CYAN}{provider_key}{RESET} {DIM}(num_ctx){RESET}")
    for mid, (cur, tgt, arch) in sorted(plan.items()):
        print(f"    {YELLOW}~ {mid}  num_ctx: {cur} → {tgt}{RESET}"
              f"  {DIM}(model max {arch}){RESET}")


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


# ── Main ──────────────────────────────────────────────────────────────────────

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
        print(f"{RED}Config not found: {config_path}{RESET}")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    providers = config.get("provider", {})
    if not providers:
        print("No providers found in config.")
        sys.exit(0)

    # ── Probe each provider ───────────────────────────────────────────────────
    pending = {}      # provider_key -> (to_add, to_remove, to_update, live_models)
    ctx_pending = {}  # provider_key -> (plan, root_url)   [--set-num-ctx, Ollama]

    for pkey, pcfg in providers.items():
        options = pcfg.get("options", {})
        base_url = options.get("baseURL", "")
        print(f"  Checking {BOLD}{pkey}{RESET} {DIM}({base_url}){RESET} ...", end=" ", flush=True)

        live = probe_provider(pkey, options)
        if live is None:
            print(f"{YELLOW}unreachable — skipped{RESET}")
            continue

        print(f"{GREEN}{len(live)} model(s){RESET}")

        config_models = pcfg.get("models", {})
        to_add, to_remove, to_update = diff_provider(config_models, live)

        if args.set_num_ctx and is_ollama(pkey, base_url.rstrip("/")):
            root = ollama_root(base_url)
            plan = ollama_ctx_plan(root, args.ctx_fraction, args.max_ctx)
            if plan:
                # The num_ctx pass owns the context value for these models;
                # drop the redundant (and stale) interim context update.
                for mid in plan:
                    to_update.pop(mid, None)
                ctx_pending[pkey] = (plan, root)

        if to_add or to_remove or to_update:
            pending[pkey] = (to_add, to_remove, to_update, live)

    # ── Show diff ─────────────────────────────────────────────────────────────
    if not pending and not ctx_pending:
        print(f"\n{GREEN}✓ Already up to date — nothing to change.{RESET}")
        return

    print(f"\n{BOLD}Proposed changes:{RESET}")
    for pkey, (to_add, to_remove, to_update, _) in pending.items():
        print_provider_diff(pkey, to_add, to_remove, to_update)
    for pkey, (plan, _) in ctx_pending.items():
        print_ctx_plan(pkey, plan)

    if args.dry_run:
        print(f"\n{DIM}(dry-run — config not modified){RESET}")
        return

    # ── Confirm + write ───────────────────────────────────────────────────────
    print()
    answer = input(f"Apply to {config_path}? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return

    # 1) Model-list changes (add / remove / context).
    for pkey, (to_add, to_remove, to_update, live) in pending.items():
        config["provider"][pkey]["models"] = apply(
            config["provider"][pkey].get("models", {}),
            live, to_add, to_remove, to_update,
        )

    # 2) num_ctx changes: re-create each model in place, then align limit.context
    #    so OpenCode never sends more than Ollama will honour.
    for pkey, (plan, root) in ctx_pending.items():
        models_cfg = config["provider"][pkey].setdefault("models", {})
        for mid, (cur, tgt, arch) in sorted(plan.items()):
            print(f"  Setting num_ctx {BOLD}{tgt}{RESET} on {mid} ...", end=" ", flush=True)
            ok, msg = set_num_ctx(root, mid, tgt)
            if ok:
                print(f"{GREEN}done{RESET}")
                if mid in models_cfg:
                    models_cfg[mid].setdefault("limit", {})["context"] = tgt
            else:
                print(f"{RED}failed — {msg}{RESET}")

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"{GREEN}✓ Config updated.{RESET}")


if __name__ == "__main__":
    main()
