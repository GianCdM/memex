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
from . import skill as skill_mod
from . import synth
from . import vault as vault_mod


def run(args) -> int:
    workspace = str(Path(getattr(args, "workspace", ".") or ".").expanduser().resolve())
    vault = config_mod.resolve_vault(getattr(args, "vault", None), workspace=workspace)
    fresh = not (vault / ".memex").exists()
    if fresh:
        print(f"creating your brain at {vault} ...")
    # create OR upgrade in place (v1 vaults gain now/, log.md, the v2 SCHEMA)
    vault_mod.ensure(vault)
    g = config_mod.load_global()
    g.setdefault("default_vault", str(vault))
    config_mod.save_global(g)

    print(f"init: workspace {workspace}  ->  vault {vault}\n")

    g = config_mod.load_global()
    g.setdefault("workspaces", {})[workspace] = str(vault)
    config_mod.save_global(g)

    if getattr(args, "hooks", True):
        hook.run(Namespace(hook_action="install", vault=str(vault), workspace=workspace))
        print()
    else:
        print("(skipped hooks — wire later with `memex hook install`)\n")

    if getattr(args, "skill", True):
        f = skill_mod.install()
        print(f"✓ memex skill installed (user-level): {f}\n")
    else:
        print("(skipped skill — install later with `memex skill install`)\n")

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

    # doc index (e.g. a tool-generated `_index.jsonl`): explicit --index, else auto-detect
    # <workspace>/_index.jsonl. Resolves local files + descriptions; PII skipped.
    index_path = getattr(args, "index", None)
    if not index_path and getattr(args, "index_auto", True):
        cand = Path(workspace) / "_index.jsonl"
        if cand.exists():
            index_path = str(cand)
    if index_path:
        print()
        ingest.run(Namespace(
            vault=str(vault), all=False, workspace=None, codebase=None, doc=None,
            docs=None, index=index_path, index_base=getattr(args, "index_base", None),
            index_mcp=getattr(args, "index_mcp", False),
            index_mcp_server=getattr(args, "index_mcp_server", None),
            source="auto", since=None, tier_override=None, session=None,
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
        print("  Session/doc pages compile automatically after each session ends.")
    print("\n  The loop from here (restart Claude Code in this workspace to activate):")
    print("    session start  → boot injects 'where we left off' (now/<workspace>.md)")
    print("    each prompt    → recall injects relevant wiki pages (deduped)")
    print("    session end    → capture + background reflect (wiki + working memory + tidy)")
    print("    deliberately   → memex search / remember / handoff (Claude knows them too)")
    print("  Peek anytime:  memex")
    return 0
