"""memex hook — install/uninstall/status the per-workspace capture + recall hooks.

Opt-in per workspace (prism model): you run `memex hook install --vault <V>` inside
a workspace and ONLY that workspace starts capturing (SessionEnd -> `memex ingest`)
and recalling (UserPromptSubmit -> `memex retrieve`), pointed at the vault you chose.
The pin in the repo's own hook config IS the routing — no global registry needed
for Claude/Cursor.

Writes `.claude/settings.local.json` (personal, gitignored) and MERGES — it never
clobbers hooks you already have. Idempotent (re-install replaces only memex's own
entries). LLM-free. Claude Code for now; Cursor/Codex are follow-ups.
"""

from __future__ import annotations

import json
from pathlib import Path

# how we recognize our own hook entries on re-install / uninstall / status
_MEMEX_TAG = "memex "


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _is_memex_group(group):
    return any(_MEMEX_TAG in (h.get("command") or "") for h in group.get("hooks", []))


def _settings_path(workspace):
    return workspace / ".claude" / "settings.local.json"


def _install(workspace, vault):
    path = _settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load_json(path)
    hooks = cfg.setdefault("hooks", {})
    plan = {
        "UserPromptSubmit": f"memex retrieve --vault {vault}",
        "SessionEnd": f"memex ingest --vault {vault} --all --workspace {workspace} --source claude",
    }
    for event, command in plan.items():
        # keep non-memex groups, replace memex's own (idempotent re-install)
        groups = [g for g in hooks.get(event, []) if not _is_memex_group(g)]
        groups.append({"hooks": [{"type": "command", "command": command}]})
        hooks[event] = groups
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path, plan


def _uninstall(workspace):
    path = _settings_path(workspace)
    cfg = _load_json(path)
    hooks = cfg.get("hooks", {})
    removed = 0
    for event in list(hooks.keys()):
        kept = [g for g in hooks[event] if not _is_memex_group(g)]
        removed += len(hooks[event]) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        cfg.pop("hooks", None)
    if path.exists():
        path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path, removed


def _status(workspace):
    path = _settings_path(workspace)
    found = []
    for event, groups in _load_json(path).get("hooks", {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if _MEMEX_TAG in (h.get("command") or ""):
                    found.append((event, h["command"]))
    return path, found


def run(args) -> int:
    workspace = Path(getattr(args, "workspace", None) or ".").expanduser().resolve()
    action = args.hook_action

    if action == "install":
        if not getattr(args, "vault", None):
            print("usage: memex hook install --vault <path> [--workspace <path>]")
            return 1
        vault = Path(args.vault).expanduser().resolve()
        if not (vault / ".memex").exists():
            print(f"error: {vault} is not a memex vault (run `memex vault new` / `memex init` first).")
            return 1
        path, plan = _install(workspace, vault)
        print(f"✓ hooks installed for workspace: {workspace}")
        print(f"  → {path}")
        print(f"  UserPromptSubmit → {plan['UserPromptSubmit']}   (auto-recall)")
        print(f"  SessionEnd       → {plan['SessionEnd']}   (capture)")
        print("  restart Claude Code in this workspace to activate.")
        return 0

    if action == "uninstall":
        path, removed = _uninstall(workspace)
        print(f"✓ removed {removed} memex hook(s) from {path}")
        return 0

    path, found = _status(workspace)
    if not found:
        print(f"no memex hooks in this workspace ({path})")
        return 0
    print(f"memex hooks in {workspace}:")
    for event, command in found:
        print(f"  {event}: {command}")
    return 0
