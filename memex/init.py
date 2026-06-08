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

    # capture the workspace's sessions + docs into raw/ (LLM-free, idempotent)
    ingest.run(Namespace(
        vault=str(vault), all=True, workspace=workspace,
        codebase=None, doc=None,
        docs=(workspace if getattr(args, "docs", True) else None),
        source="auto", since=getattr(args, "since", None),
        tier_override=None, session=None,
    ))

    # extra doc roots (e.g. a locally-synced Drive folder) — opt-in via --docs-from.
    # Captures the REAL files there (docx/pdf/pptx/images); cloud-native stubs
    # (.gdoc/.gsheet) are refused — those need an MCP export to Markdown first.
    for root in (getattr(args, "docs_from", None) or []):
        ingest.run(Namespace(
            vault=str(vault), all=False, workspace=None, codebase=None, doc=None,
            docs=root, source="auto", since=None, tier_override=None, session=None,
        ))

    # code: build architecture hubs (ON by default — it's the 3rd ingest, and it's
    # BOUNDED: one overview per repo, not per file. --no-analyze to skip.)
    if getattr(args, "analyze", True):
        print()
        analyze.run(Namespace(
            repo=workspace, vault=str(vault),
            provider=getattr(args, "provider", None), modules=None, model_merge=None))

    # compile sessions/docs raw -> wiki (opt-in — scales with your backlog)
    if getattr(args, "synth", False):
        print()
        synth.run(Namespace(
            vault=str(vault), provider=getattr(args, "provider", None),
            limit=getattr(args, "limit", None), since=None,
            model_propose=None, model_merge=None,
        ))

    print(f"\n✓ memex is live. Your brain: {vault}")
    print("  Captured into raw/: this workspace's sessions" +
          (" + docs." if getattr(args, "docs", True) else " (docs skipped)."))
    if getattr(args, "analyze", True):
        print("  Built: code architecture hubs (one per repo).")
    if not getattr(args, "synth", False):
        print("  Session/doc pages compile as you work (SessionEnd hook), or now with:")
        print(f"    memex synth --vault {vault}")
    print(f"  Peek anytime:  memex status --vault {vault}")
    return 0
