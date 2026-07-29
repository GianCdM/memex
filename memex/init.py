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

    # Re-init on an already-wired workspace: just ensure MCP + hooks are current
    # and exit — no need to re-ingest or re-analyze.
    already_wired = False
    try:
        cfg = hook._load_json(hook._settings_path(Path(workspace)))
        for groups in cfg.get("hooks", {}).values():
            for g in groups:
                for h in g.get("hooks", []):
                    if hook._is_memex_command(h.get("command")):
                        already_wired = True
                        break
    except Exception:
        pass

    if already_wired:
        print(f"memex is already active in this workspace → {vault}")
        if "memex" not in cfg.get("mcpServers", {}):
            hook._install_mcp(Path(workspace))
            print("✓ MCP server wired (memex tools now available to Claude)")
        else:
            print("  hooks + MCP server: up to date")
        # Still refresh the skill — it may have been updated
        if getattr(args, "skill", True):
            skill_mod.install()
        print("  (re-run without --no-hooks --no-skill to skip this check)")
        return 0

    if fresh:
        print(f"creating your brain at {vault} ...")
    # create OR upgrade in place (v1 vaults gain now/, log.md, the v2 SCHEMA)
    total_steps = 3 + (1 if getattr(args, "analyze", True) else 0) \
                    + (1 if getattr(args, "synth", False) else 0)
    step = iter(range(1, total_steps + 1))

    def phase(label):
        print(f"[{next(step)}/{total_steps}] {label}")

    phase(f"vault  →  {vault}")
    vault_mod.ensure(vault)
    # mutate only the USER's config file (not the defaults-merged view — saving
    # that would freeze shipped defaults into the user's file forever)
    g = config_mod.load_user()
    g.setdefault("default_vault", str(vault))
    g.setdefault("workspaces", {})[workspace] = str(vault)
    config_mod.save_global(g)
    print(f"       workspace {workspace} registered\n")

    phase("hooks + skill + MCP (the automatic loop)")
    if getattr(args, "hooks", True):
        hook.run(Namespace(hook_action="install", vault=str(vault), workspace=workspace))
        hook._install_mcp(Path(workspace))
        print("✓ MCP server wired (memex tools available to Claude)\n")
    else:
        print("(skipped hooks — wire later with `memex hook install`)")
    if getattr(args, "skill", True):
        f = skill_mod.install()
        print(f"✓ memex skill installed (user-level): {f}\n")
    else:
        print("(skipped skill — install later with `memex skill install`)\n")

    # capture THIS workspace's past sessions + docs into raw/ (LLM-free,
    # idempotent). Scope is deliberate: the brain only ever captures where you
    # explicitly ran `memex init` — activation is a per-workspace opt-in.
    # The vault itself is excluded from the doc scan (a vault inside the
    # workspace must never eat its own output), and a home-root workspace
    # skips the doc scan entirely (recursing the whole profile is never what
    # anyone means — point --docs-from at the actual folders instead).
    phase("capturing this workspace's past (sessions + docs — LLM-free)")
    scan_docs = getattr(args, "docs", True)
    if scan_docs and Path(workspace) == Path.home():
        print("(workspace is your home directory — skipping the doc scan; use "
              "--docs-from <folder> for the folders you actually want)\n")
        scan_docs = False
    ingest.run(Namespace(
        vault=str(vault), all=True, workspace=workspace, doc=None,
        docs=(workspace if scan_docs else None),
        source="auto", since=getattr(args, "since", None),
        tier_override=None, session=None, exclude=str(vault),
    ))

    # extra doc roots (e.g. a locally-synced Drive folder) — opt-in via --docs-from.
    # Captures the REAL files there (docx/pdf/pptx/images); cloud-native stubs
    # (.gdoc/.gsheet) are refused — those need an MCP export to Markdown first.
    for root in (getattr(args, "docs_from", None) or []):
        ingest.run(Namespace(
            vault=str(vault), all=False, workspace=None, doc=None,
            docs=root, source="auto", since=None, tier_override=None, session=None,
            exclude=str(vault),
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
            vault=str(vault), all=False, workspace=None, doc=None,
            docs=None, index=index_path, index_base=getattr(args, "index_base", None),
            index_mcp=getattr(args, "index_mcp", False),
            index_mcp_server=getattr(args, "index_mcp_server", None),
            source="auto", since=None, tier_override=None, session=None,
        ))

    # code: build architecture hubs (ON by default — it's the 3rd ingest, and it's
    # BOUNDED: one overview per repo, not per file. --no-analyze to skip.)
    if getattr(args, "analyze", True):
        print()
        phase("architecture pages for this workspace's code (LLM)")
        analyze.run(Namespace(
            repo=workspace, vault=str(vault),
            provider=getattr(args, "provider", None), modules=None, model_merge=None))

    # compile sessions/docs raw -> wiki (opt-in — scales with your backlog)
    if getattr(args, "synth", False):
        print()
        phase("compiling the whole backlog into the wiki (LLM)")
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
    print("    deliberately   → MCP tools: search, remember, status (Claude uses them)")
    print("  Peek anytime:  memex")
    return 0
