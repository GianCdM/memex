"""memex CLI — command router (argparse, stdlib only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import analyze
from . import boot as boot_mod
from . import capture as capture_mod
from . import config as config_mod
from . import doctor
from . import embed as embed_mod
from . import gardening
from . import hook
from . import ingest
from . import init as init_mod
from . import mcp_server
from . import recall as recall_mod
from . import reflect as reflect_mod
from . import relink as relink_mod
from . import search as search_mod
from . import skill as skill_mod
from . import synth
from . import vault


def _tidy_cmd(args) -> int:
    args.vault = str(config_mod.resolve_vault(getattr(args, "vault", None)))
    return gardening.run(args)


def _config_cmd(args) -> int:
    if args.action == "get":
        cur = config_mod.load_global()  # defaults merged in — what's in effect
        if args.key:
            for part in args.key.split("."):
                cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        print(json.dumps(cur, indent=2))
        return 0
    if not args.key or args.value is None:
        print("usage: memex config set <key.path> <value>")
        return 1
    # SET mutates only the user's own file — never the merged view, or every
    # shipped default (model names, base_urls) gets frozen into the user file
    # and future memex upgrades can't move them.
    g = config_mod.load_user()
    effective = config_mod.load_global()
    cur, eff, parts = g, effective, args.key.split(".")
    for part in parts[:-1]:
        eff = eff.get(part, {}) if isinstance(eff, dict) else {}
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            print(f"error: '{part}' is not a section.")
            return 1
    if isinstance(eff, dict) and isinstance(eff.get(parts[-1]), (dict, list)):
        print(f"error: '{args.key}' is a section/list, not a value — refusing to "
              "overwrite it with a scalar (edit the JSON file directly if you mean it).")
        return 1
    val = args.value
    if val.lower() in ("true", "false"):
        val = val.lower() == "true"
    elif val.lower() in ("null", "none"):
        val = None
    else:
        try:
            val = int(val)
        except ValueError:
            pass
    cur[parts[-1]] = val
    config_mod.save_global(g)
    print(f"set {args.key} = {val}")
    return 0


def _status_cmd(args) -> int:
    vault_dir = config_mod.resolve_vault(getattr(args, "vault", None))
    mx = vault_dir / ".memex"
    if not mx.exists():
        print(f"error: {vault_dir} is not a memex vault.")
        return 1
    raw = list((vault_dir / "raw").glob("*.md"))
    try:
        pages = json.loads((mx / "index.json").read_text(encoding="utf-8")).get("pages", [])
    except Exception:
        pages = []
    try:
        synthed = json.loads((mx / "synthed.json").read_text(encoding="utf-8"))
    except Exception:
        synthed = {}
    kinds: dict[str, int] = {}
    for p in pages:
        k = p.get("kind", "session")
        kinds[k] = kinds.get(k, 0) + 1
    statuses: dict[str, int] = {}
    for p in pages:
        s = p.get("status", "current")
        statuses[s] = statuses.get(s, 0) + 1
    workspace_pages = sorted((vault_dir / "workspace").glob("*.md")) if (vault_dir / "workspace").is_dir() else []
    print(f"vault: {vault_dir}")
    print(f"  raw notes  : {len(raw)}")
    print(f"  synthesized: {len(synthed)}  (pending: {max(0, len(raw) - len(synthed))})")
    print(f"  wiki pages : {len(pages)}  kinds={dict(kinds)}  statuses={dict(statuses)}")
    if workspace_pages:
        print(f"  working mem: {len(workspace_pages)}  ({', '.join(p.stem for p in workspace_pages[:6])})")
    sug = vault_dir / "wiki" / "_sugestoes.md"
    if sug.exists():
        n = sum(1 for ln in sug.read_text(encoding="utf-8").splitlines() if ln.startswith("## "))
        if n:
            print(f"  suggestions: {n}  (open wiki/_sugestoes.md in Obsidian)")
    return 0


def _log_cmd(args) -> int:
    vault_dir = config_mod.resolve_vault(getattr(args, "vault", None))
    cl = vault_dir / ".memex" / "changelog.jsonl"
    if not cl.exists():
        print("no changelog yet.")
        return 0
    rows = []
    for ln in cl.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            pass
    if args.page:
        rows = [r for r in rows if r.get("page") == args.page]
    from datetime import datetime
    for r in rows[-(args.limit or 50):]:
        try:
            ts = datetime.fromtimestamp(int(r.get("ts", 0))).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            ts = str(r.get("ts"))
        origin = r.get("source") or (
            "absorbed: " + ", ".join(r.get("absorbed", [])) if r.get("absorbed") else "")
        print(f"  {ts}  [{r.get('tier')}] {r.get('action'):12}  {r.get('page')}"
              + (f"  <- {origin}" if origin else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memex",
        description="A portable, local-first second brain built from your AI sessions — "
                    "management, architecture and coding alike.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "the one command you need:\n"
            "  memex init       set up the current workspace, once. After that the brain\n"
            "                   runs itself: capture, synthesis, working memory, recall,\n"
            "                   consolidation — all automatic, via hooks.\n"
            "\n"
            "talk to the brain (you or your agent — for management work as much as code):\n"
            "  memex search     find pages (scored, with file paths)\n"
            "\n"
            "peek anytime:\n"
            "  memex            (no args) what's in your brain\n"
            "  memex doctor     check your setup (provider, hooks, skill)\n"
            "\n"
            "everything else (boot / recall / capture / reflect / tidy / synth / ...) is\n"
            "plumbing the hooks run for you — hidden from this help on purpose.\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── porcelain: what a human actually runs ─────────────────────────────
    pi = sub.add_parser(
        "init", help="set up this workspace: hooks + skill + capture backlog into the brain")
    pi.add_argument("--vault", help="where the brain lives (default: ~/memex, or this workspace's vault)")
    pi.add_argument("--workspace", default=".")
    pi.add_argument("--no-analyze", dest="analyze", action="store_false",
                    help="don't build code architecture pages")
    pi.add_argument("--synth", action="store_true",
                    help="also compile the whole session/doc backlog into the wiki now (LLM)")
    pi.add_argument("--no-docs", dest="docs", action="store_false",
                    help="don't adopt this workspace's documents")
    pi.add_argument("--docs-from", action="append", default=[], metavar="PATH",
                    help="also adopt docs from an extra folder/glob (repeatable) — e.g. a synced Drive folder")
    pi.add_argument("--index", metavar="JSONL",
                    help="ingest a doc index (default: auto-detect <workspace>/_index.jsonl)")
    pi.add_argument("--index-base", dest="index_base", metavar="DIR",
                    help="content root for the index's relative paths (default: auto-probe near the index)")
    pi.add_argument("--no-index", dest="index_auto", action="store_false",
                    help="don't auto-ingest a workspace doc index")
    pi.add_argument("--index-mcp", dest="index_mcp", action="store_true",
                    help="resolve the index's cloud-native docs via the provider's MCP")
    pi.add_argument("--index-mcp-server", dest="index_mcp_server", metavar="NAME",
                    help="MCP server for the index's read tools (e.g. google-workspace)")
    pi.add_argument("--no-hooks", dest="hooks", action="store_false",
                    help="don't install the boot/recall/capture hooks")
    pi.add_argument("--no-skill", dest="skill", action="store_false",
                    help="don't install the user-level Claude Code skill")
    pi.add_argument("--since")
    pi.add_argument("--provider")
    pi.add_argument("--limit", type=int)
    pi.set_defaults(func=init_mod.run)

    pstat = sub.add_parser("status", help="peek at the brain (pages, pending, working memory)")
    pstat.add_argument("--vault")
    pstat.set_defaults(func=_status_cmd)

    pdoc = sub.add_parser("doctor", help="detect environment + recommend provider/model setup")
    pdoc.set_defaults(func=doctor.run)

    psearch = sub.add_parser("search", help="find pages in the brain (scored, with paths)")
    psearch.add_argument("terms", nargs="*", metavar="TERMS")
    psearch.add_argument("--vault")
    psearch.add_argument("--limit", type=int, default=10)
    psearch.set_defaults(func=search_mod.run)

    # code architecture pages — init builds them; re-run by hand after a big
    # refactor (auto-refresh is on the roadmap)
    pan = sub.add_parser("analyze")
    pan.add_argument("repo", nargs="?", default=".")
    pan.add_argument("--vault")
    pan.add_argument("--provider")
    pan.add_argument("--modules", type=int, help="max module pages (default 6; 0 = overview only)")
    pan.add_argument("--model-merge", dest="model_merge")
    pan.set_defaults(func=analyze.run)

    # ── plumbing: the hooks call these; a human rarely does (hidden from --help) ──
    pboot = sub.add_parser("boot")  # SessionStart
    pboot.add_argument("--vault")
    pboot.set_defaults(func=boot_mod.run)

    precall = sub.add_parser("recall")  # UserPromptSubmit
    precall.add_argument("--vault")
    precall.add_argument("--query")
    precall.set_defaults(func=recall_mod.run)

    # v1 alias — old installed hooks say `memex retrieve`
    pret = sub.add_parser("retrieve")
    pret.add_argument("--vault")
    pret.add_argument("--query")
    pret.set_defaults(func=recall_mod.run)

    pcap = sub.add_parser("capture")  # SessionEnd / PreCompact
    pcap.add_argument("--vault")
    pcap.add_argument("--workspace")
    pcap.add_argument("--transcript", help="transcript path (default: from the hook payload)")
    pcap.add_argument("--partial", action="store_true",
                      help="PreCompact mode: ingest only, no reflect")
    pcap.add_argument("--docs", action="store_true",
                      help="also refresh this workspace's docs")
    pcap.add_argument("--no-reflect", dest="no_reflect", action="store_true")
    pcap.set_defaults(func=capture_mod.run)

    prefl = sub.add_parser("reflect")  # detached post-session worker
    prefl.add_argument("--vault", required=True)
    prefl.add_argument("--cwd")
    prefl.add_argument("--since")
    prefl.add_argument("--limit", type=int)
    prefl.add_argument("--provider")
    prefl.add_argument("--workers", type=int, default=None,
                       help="parallel LLM workers for the synth phase")
    prefl.set_defaults(func=reflect_mod.run)

    pv = sub.add_parser("vault")
    pv.set_defaults(func=lambda a: (pv.print_help() or 0))
    vs = pv.add_subparsers(dest="vault_command", metavar="<subcommand>")
    pvn = vs.add_parser("new", help="scaffold a clean vault")
    pvn.add_argument("path")
    pvn.set_defaults(func=vault.new)

    pg = sub.add_parser("ingest")
    pg.add_argument("--vault", required=True)
    pg.add_argument("--all", action="store_true")
    pg.add_argument("--doc", metavar="FILE")
    pg.add_argument("--exclude", metavar="DIR",
                    help="path pruned from the --docs walk (init/capture pass the vault)")
    pg.add_argument("--docs", metavar="DIR_OR_GLOB",
                    help="adopt a folder/glob of Markdown docs (external tool output, /docs, notes)")
    pg.add_argument("--index", metavar="JSONL",
                    help="ingest from a doc index (jsonl of locators; resolves local + cloud refs)")
    pg.add_argument("--index-base", dest="index_base", metavar="DIR",
                    help="content root for the index's relative paths (default: auto-probe near the index)")
    pg.add_argument("--index-mcp", dest="index_mcp", action="store_true",
                    help="also resolve cloud-native docs via the provider's MCP (best-effort)")
    pg.add_argument("--index-mcp-server", dest="index_mcp_server", metavar="NAME",
                    help="MCP server that serves the index's read tools (e.g. google-workspace)")
    pg.add_argument("--source", choices=["auto", "claude", "cursor", "codex"], default="auto")
    pg.add_argument("--workspace")
    pg.add_argument("--since")
    pg.set_defaults(func=ingest.run)

    ps = sub.add_parser("synth")
    ps.add_argument("--vault", required=True)
    ps.add_argument("--provider")
    ps.add_argument("--limit", type=int)
    ps.add_argument("--since")
    ps.add_argument("--only", help="synthesize a single raw note by filename")
    ps.add_argument("--model-propose", dest="model_propose")
    ps.add_argument("--model-merge", dest="model_merge")
    ps.add_argument("--workers", type=int, default=None,
                    help="parallel LLM workers (default: from limits, or 1)")
    ps.set_defaults(func=synth.run)

    ph = sub.add_parser("hook")
    ph.add_argument("hook_action", choices=["install", "uninstall", "status"])
    ph.add_argument("--vault")
    ph.add_argument("--workspace", default=".")
    ph.set_defaults(func=hook.run)

    pmcp = sub.add_parser("mcp", help="start the MCP server (for AI agent tool access)")
    pmcp.set_defaults(func=mcp_server.run)

    psk = sub.add_parser("skill")
    psk.add_argument("skill_action", choices=["install", "uninstall", "status"],
                     nargs="?", default="status")
    psk.set_defaults(func=skill_mod.run)

    # tidy = consolidation of near-duplicate pages. Runs AUTOMATICALLY via
    # reflect on a cadence; the manual verb exists for tuning ("gardening" kept
    # as a hidden legacy alias).
    for tidy_name in ("tidy", "gardening"):
        pgd = sub.add_parser(tidy_name)
        pgd.add_argument("--vault")
        pgd.add_argument("--threshold", type=float)
        pgd.add_argument("--dry-run", dest="dry_run", action="store_true")
        pgd.add_argument("--provider")
        pgd.add_argument("--model-merge", dest="model_merge")
        pgd.set_defaults(func=_tidy_cmd)

    pemb = sub.add_parser("embed",
                          help="precompute vector embeddings for semantic recall (optional)")
    pemb.add_argument("--vault")
    pemb.add_argument("--force", action="store_true",
                      help="re-embed everything, ignore cache")
    pemb.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="show what would run, don't call the endpoint")
    pemb.set_defaults(func=embed_mod.run)

    prl = sub.add_parser("relink",
                          help="retroactively add wikilinks to orphan pages")
    prl.add_argument("--vault")
    prl.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="show what would change, don't write")
    prl.add_argument("--all", action="store_true",
                     help="target ALL under-linked pages, not just orphans")
    prl.add_argument("--refresh", action="store_true",
                     help="also refresh pages that already have a relink block (use after "
                          "switching scorers, e.g. turning embeddings on)")
    prl.add_argument("--min-links", dest="min_links", type=int, default=2,
                     help="threshold when --all is used (default: 2)")
    prl.add_argument("--top-k", dest="top_k", type=int, default=4,
                     help="how many wikilinks to add per page (default: 4)")
    prl.set_defaults(func=relink_mod.run)

    pc = sub.add_parser("config")
    pc.add_argument("action", choices=["get", "set"])
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_config_cmd)

    plog = sub.add_parser("log")
    plog.add_argument("--vault")
    plog.add_argument("--page")
    plog.add_argument("--limit", type=int, default=50)
    plog.set_defaults(func=_log_cmd)

    return parser


def main(argv=None) -> int:
    # Windows consoles default to a legacy codepage (cp1252) that can't encode
    # "→"/"✓"/emoji — and the harness speaks UTF-8 on BOTH ends: hook payloads
    # (accented prompts!) arrive on stdin, injected context leaves on stdout.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "command", None):
        # bare `memex` = "how's my brain?" when one exists; help otherwise
        vault_dir = config_mod.resolve_vault(None)
        if (vault_dir / ".memex").exists():
            return _status_cmd(argparse.Namespace(vault=str(vault_dir)))
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
