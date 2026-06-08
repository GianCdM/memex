"""memex init — onboard a workspace into a vault (orchestrator).

Run inside a workspace. Registers workspace -> vault, installs the capture +
recall hooks (unless --no-hooks), backfills this workspace's sessions + codebase
into the vault, and optionally synthesizes. One command to go live.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from . import analyze
from . import config as config_mod
from . import hook
from . import ingest
from . import synth
from . import vault as vault_mod


def _resolve_vault(args):
    """Pick the vault without making the user think about it:
    explicit --vault > this workspace's registered vault > global default > ~/memex."""
    if getattr(args, "vault", None):
        return Path(args.vault).expanduser().resolve()
    workspace = str(Path(getattr(args, "workspace", ".") or ".").expanduser().resolve())
    g = config_mod.load_global()
    mapped = g.get("workspaces", {}).get(workspace)
    if mapped:
        return Path(mapped).expanduser().resolve()
    if g.get("default_vault"):
        return Path(g["default_vault"]).expanduser().resolve()
    return Path("~/memex").expanduser().resolve()


def run(args) -> int:
    vault = _resolve_vault(args)
    if not (vault / ".memex").exists():
        print(f"creating your brain at {vault} ...")
        vault_mod.new(Namespace(path=str(vault), tier="personal"))
        g = config_mod.load_global()
        g.setdefault("default_vault", str(vault))
        config_mod.save_global(g)
        print()

    workspace = str(Path(getattr(args, "workspace", ".") or ".").expanduser().resolve())
    print(f"init: workspace {workspace}  ->  vault {vault}\n")

    g = config_mod.load_global()
    g.setdefault("workspaces", {})[workspace] = str(vault)
    config_mod.save_global(g)

    if getattr(args, "hooks", True):
        hook.run(Namespace(hook_action="install", vault=str(vault), workspace=workspace))
        print()
    else:
        print("(skipped hooks — wire later with `memex hook install`)\n")

    ingest.run(Namespace(
        vault=str(vault), all=True, workspace=workspace,
        codebase=(workspace if getattr(args, "codebase", False) else None),
        doc=None, source="auto", since=getattr(args, "since", None),
        tier_override=None, session=None,
    ))

    if getattr(args, "synth", True):
        print()
        synth.run(Namespace(
            vault=str(vault), provider=getattr(args, "provider", None),
            limit=getattr(args, "limit", None), since=None,
            model_propose=None, model_merge=None,
        ))
    else:
        print("\n(skipped synth — the brain compiles on the next run)")

    if getattr(args, "analyze", False):
        print()
        analyze.run(Namespace(
            repo=workspace, vault=str(vault),
            provider=getattr(args, "provider", None), modules=None, model_merge=None))

    print(f"\n✓ memex is live. Your brain: {vault}")
    print("  Open it in Obsidian. From now on memex runs itself —")
    print("  each session is captured, compiled, and recalled automatically.")
    print(f"  Peek anytime with:  memex status --vault {vault}")
    return 0
