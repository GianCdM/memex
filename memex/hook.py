"""memex hook — install/uninstall/status the per-workspace brain hooks.

Opt-in per workspace (prism model): `memex hook install --vault <V>` inside a
workspace wires ONLY that workspace, pointed at the vault you chose. The pin in
the repo's own hook config IS the routing — no global registry needed.

v2 wires the full memory loop across four lifecycle events:

  SessionStart     -> memex boot      inject working memory ("where we left off")
  UserPromptSubmit -> memex recall    inject relevant wiki pages (deduped/session)
  SessionEnd       -> memex capture   ingest THIS transcript + spawn detached reflect
  PreCompact       -> memex capture   same as SessionEnd — save, synth, workspace, tidy, embed

Portability rules (v1 broke all three on Windows):
- absolute path to the memex executable — hooks can't trust the harness PATH;
- no shell substitutions ($(date ...)), no `nohup ... &` — detaching happens
  inside `memex capture` via proc.spawn_detached;
- double quotes only (understood by cmd, PowerShell and Git Bash alike).

Writes `.claude/settings.local.json` (personal, gitignored) and MERGES — it
never clobbers hooks you already have. Idempotent (re-install replaces only
memex's own entries, including v1-era ones). LLM-free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import proc

# how we recognize our own hook entries on re-install / uninstall / status:
# a memex invocation = the word/path `memex` followed by one of OUR verbs.
# A bare-substring match would silently delete a user's unrelated hook that
# merely MENTIONS memex (e.g. `echo "memex backup done"`).
_MEMEX_CMD_RE = re.compile(
    r'(^|[\s"/\\])memex(\.exe)?"?\s+'
    r"(boot|recall|retrieve|capture|reflect|ingest|synth|tidy|gardening)\b",
    re.IGNORECASE,
)


def _is_memex_command(command: str) -> bool:
    return bool(_MEMEX_CMD_RE.search(command or ""))


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _is_memex_group(group):
    return any(_is_memex_command(h.get("command")) for h in group.get("hooks", []))


def _settings_path(workspace):
    return workspace / ".claude" / "settings.local.json"


def build_plan(vault) -> dict:
    """event -> hook command. Absolute exe, ALWAYS double-quoted, forward
    slashes: Git Bash eats unquoted backslashes (C:\\Users -> C:Users), cmd
    accepts quoted forward-slash paths — this form works in both."""
    exe = proc.memex_exe().replace("\\", "/")
    v = str(vault).replace("\\", "/")
    # quote the exe ONLY if it has whitespace: a leading quote makes cmd.exe's
    # `/c "..."` strip quotes wrongly, and an unquoted forward-slash path is
    # valid in Git Bash AND cmd — the safest common denominator.
    base = f'"{exe}"' if re.search(r"\s", exe) else exe

    def cmd(verb, extra=""):
        return f'{base} {verb} --vault "{v}"{extra}'

    return {
        "SessionStart": cmd("boot"),
        "UserPromptSubmit": cmd("recall"),
        "SessionEnd": cmd("capture", " --docs"),
        "PreCompact": cmd("capture", " --partial"),
    }


def _install(workspace, vault):
    path = _settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load_json(path)
    hooks = cfg.setdefault("hooks", {})
    plan = build_plan(vault)
    # drop memex groups from EVERY event first (also cleans up v1-era entries
    # under events we no longer use), then add the current plan
    for event in list(hooks.keys()):
        kept = [g for g in hooks[event] if not _is_memex_group(g)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    for event, command in plan.items():
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": command}]}
        )
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
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
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return path, removed


def _status(workspace):
    path = _settings_path(workspace)
    found = []
    for event, groups in _load_json(path).get("hooks", {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if _is_memex_command(h.get("command")):
                    found.append((event, h["command"]))
    return path, found


def _install_mcp(workspace):
    """Add the memex MCP server to the workspace's Claude Code settings.
    Idempotent — replaces any existing memex MCP entry, preserves others."""
    path = _settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load_json(path)
    servers = cfg.setdefault("mcpServers", {})
    exe = proc.memex_exe().replace("\\", "/")
    servers["memex"] = {
        "command": exe,
        "args": ["mcp"],
    }
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _uninstall_mcp(workspace):
    """Remove the memex MCP server entry from workspace settings."""
    path = _settings_path(workspace)
    cfg = _load_json(path)
    if "memex" in cfg.get("mcpServers", {}):
        del cfg["mcpServers"]["memex"]
        if not cfg["mcpServers"]:
            del cfg["mcpServers"]
        if path.exists():
            path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return True
    return False


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
        # register the workspace -> vault mapping too: the hooks carry their own
        # --vault pin, but the vault-less porcelain the skill uses in-session
        # (search) resolves through this registry — without it
        # those verbs would talk to the DEFAULT brain, splitting memories.
        from . import config as config_mod
        g = config_mod.load_user()
        g.setdefault("workspaces", {})[str(workspace)] = str(vault)
        config_mod.save_global(g)
        print(f"✓ brain hooks installed for workspace: {workspace}")
        print(f"  → {path}")
        print("  SessionStart     → boot     (inject working memory: where we left off)")
        print("  UserPromptSubmit → recall   (inject relevant wiki pages, deduped)")
        print("  SessionEnd       → capture  (save + reflect: synth, workspace, tidy, embed)")
        print("  PreCompact       → capture  (same as SessionEnd)")
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
    for event, command in sorted(found):
        print(f"  {event}: {command}")
    return 0
