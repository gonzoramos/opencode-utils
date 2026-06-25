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


def discover_ollama(base_url: str) -> dict | None:
    """Return {model_id: entry_dict} from Ollama."""
    data = fetch(f"{base_url}/v1/models")
    if data is None:
        return None

    models = {}
    for m in data.get("data", []):
        mid = m["id"]
        details = fetch(f"{base_url}/api/show", method="POST", body={"name": mid})
        if not details:
            continue

        # Context length lives under <arch>.context_length in model_info
        model_info = details.get("model_info", {})
        ctx_key = next((k for k in model_info if k.endswith(".context_length")), None)
        ctx = model_info[ctx_key] if ctx_key else 32768

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


def probe_provider(provider_key: str, options: dict) -> dict | None:
    """
    Pick the right discovery function based on provider key / base URL.
    Returns None if the server is unreachable.
    """
    base_url = options.get("baseURL", "").rstrip("/")
    if not base_url:
        return None

    if "11434" in base_url or "ollama" in provider_key.lower():
        root = base_url.replace("/v1", "")
        return discover_ollama(root)
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
    pending = {}   # provider_key -> (to_add, to_remove, to_update, live_models)

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

        if to_add or to_remove or to_update:
            pending[pkey] = (to_add, to_remove, to_update, live)

    # ── Show diff ─────────────────────────────────────────────────────────────
    if not pending:
        print(f"\n{GREEN}✓ Already up to date — nothing to change.{RESET}")
        return

    print(f"\n{BOLD}Proposed changes:{RESET}")
    for pkey, (to_add, to_remove, to_update, _) in pending.items():
        print_provider_diff(pkey, to_add, to_remove, to_update)

    if args.dry_run:
        print(f"\n{DIM}(dry-run — config not modified){RESET}")
        return

    # ── Confirm + write ───────────────────────────────────────────────────────
    print()
    answer = input(f"Apply to {config_path}? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return

    for pkey, (to_add, to_remove, to_update, live) in pending.items():
        config["provider"][pkey]["models"] = apply(
            config["provider"][pkey].get("models", {}),
            live, to_add, to_remove, to_update,
        )

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"{GREEN}✓ Config updated.{RESET}")


if __name__ == "__main__":
    main()
