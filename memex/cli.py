"""memex CLI — command router (argparse, stdlib only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import analyze
from . import config as config_mod
from . import doctor
from . import gardening
from . import hook
from . import ingest
from . import init as init_mod
from . import retrieve
from . import synth
from . import vault


def _stub(name: str):
    def run(args):
        print(f"memex {name}: planned, not implemented yet. See the README roadmap.")
        return 0

    return run


def _config_cmd(args) -> int:
    g = config_mod.load_global()
    if args.action == "get":
        cur = g
        if args.key:
            for part in args.key.split("."):
                cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        print(json.dumps(cur, indent=2))
        return 0
    if not args.key or args.value is None:
        print("usage: memex config set <key.path> <value>")
        return 1
    cur, parts = g, args.key.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    val = args.value
    if val.lower() in ("true", "false"):
        val = val.lower() == "true"
    elif val.lower() in ("null", "none"):
        val = None
    cur[parts[-1]] = val
    config_mod.save_global(g)
    print(f"set {args.key} = {val}")
    return 0


def _status_cmd(args) -> int:
    vault_dir = Path(args.vault).expanduser().resolve()
    mx = vault_dir / ".memex"
    if not mx.exists():
        print(f"error: {vault_dir} is not a memex vault.")
        return 1
    raw = list((vault_dir / "raw").glob("*.md"))
    try:
        pages = json.loads((mx / "index.json").read_text()).get("pages", [])
    except Exception:
        pages = []
    try:
        synthed = json.loads((mx / "synthed.json").read_text())
    except Exception:
        synthed = {}
    tiers: dict[str, int] = {}
    for p in pages:
        t = p.get("tier", "silver")
        tiers[t] = tiers.get(t, 0) + 1
    print(f"vault: {vault_dir}")
    print(f"  raw notes  : {len(raw)}")
    print(f"  synthesized: {len(synthed)}  (pending: {max(0, len(raw) - len(synthed))})")
    print(f"  wiki pages : {len(pages)}  {dict(tiers)}")
    sug = vault_dir / "wiki" / "_sugestoes.md"
    if sug.exists():
        n = sum(1 for ln in sug.read_text().splitlines() if ln.startswith("## "))
        if n:
            print(f"  suggestions: {n}  (open wiki/_sugestoes.md in Obsidian)")
    return 0


def _log_cmd(args) -> int:
    cl = Path(args.vault).expanduser().resolve() / ".memex" / "changelog.jsonl"
    if not cl.exists():
        print("no changelog yet.")
        return 0
    rows = []
    for ln in cl.read_text().splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            pass
    if args.page:
        rows = [r for r in rows if r.get("page") == args.page]
    for r in rows[-(args.limit or 50):]:
        print(f"  {r.get('ts')}  [{r.get('tier')}] {r.get('action'):6}  {r.get('page')}  <- {r.get('source')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memex",
        description="A portable, local-first second brain built from your AI coding sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "the one command you need:\n"
            "  memex init       set up the current workspace — build your brain and turn on\n"
            "                   automatic capture + recall (run once per workspace)\n"
            "\n"
            "peek anytime:\n"
            "  memex status     what's in your brain (pages, pending, suggestions)\n"
            "  memex doctor     check your setup (provider, hooks)\n"
            "\n"
            "after `memex init`, memex runs itself: each session is captured, compiled into\n"
            "the wiki, and recalled into new sessions automatically. the low-level commands\n"
            "the hooks call (ingest / synth / retrieve / ...) are hidden on purpose.\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── porcelain: what a human actually runs ─────────────────────────────
    pi = sub.add_parser(
        "init", help="set up this workspace: capture sessions + docs, wire auto capture/recall")
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
    pi.add_argument("--no-index", dest="index_auto", action="store_false",
                    help="don't auto-ingest a workspace doc index")
    pi.add_argument("--index-mcp", dest="index_mcp", action="store_true",
                    help="resolve the index's cloud-native docs via the provider's MCP")
    pi.add_argument("--no-hooks", dest="hooks", action="store_false",
                    help="don't install the capture + recall hooks")
    pi.add_argument("--since")
    pi.add_argument("--provider")
    pi.add_argument("--limit", type=int)
    pi.set_defaults(func=init_mod.run)

    pstat = sub.add_parser("status", help="peek at the brain (pages, pending, suggestions)")
    pstat.add_argument("--vault", required=True)
    pstat.set_defaults(func=_status_cmd)

    pdoc = sub.add_parser("doctor", help="detect environment + recommend provider/model setup")
    pdoc.set_defaults(func=doctor.run)

    pan = sub.add_parser("analyze", help="synthesize a codebase into a few architecture pages (C4-style)")
    pan.add_argument("repo", nargs="?", default=".")
    pan.add_argument("--vault")
    pan.add_argument("--provider")
    pan.add_argument("--modules", type=int, help="max module pages (default 6; 0 = overview only)")
    pan.add_argument("--model-merge", dest="model_merge")
    pan.set_defaults(func=analyze.run)

    # ── plumbing: the hooks call these; a human rarely does (hidden from --help) ──
    pv = sub.add_parser("vault")
    pv.set_defaults(func=lambda a: (pv.print_help() or 0))
    vs = pv.add_subparsers(dest="vault_command", metavar="<subcommand>")
    pvn = vs.add_parser("new", help="scaffold a clean vault")
    pvn.add_argument("path")
    pvn.add_argument("--tier", choices=["personal", "work"], default="personal")
    pvn.set_defaults(func=vault.new)

    pg = sub.add_parser("ingest")
    pg.add_argument("--vault", required=True)
    pg.add_argument("--all", action="store_true")
    pg.add_argument("--codebase", nargs="?", const=".", default=None, metavar="PATH")
    pg.add_argument("--doc", metavar="FILE")
    pg.add_argument("--docs", metavar="DIR_OR_GLOB",
                    help="adopt a folder/glob of Markdown docs (external tool output, /docs, notes)")
    pg.add_argument("--index", metavar="JSONL",
                    help="ingest from a doc index (jsonl of locators; resolves local + cloud refs)")
    pg.add_argument("--index-mcp", dest="index_mcp", action="store_true",
                    help="also resolve cloud-native docs via the provider's MCP (best-effort)")
    pg.add_argument("--source", choices=["auto", "claude", "cursor", "codex"], default="auto")
    pg.add_argument("--workspace")
    pg.add_argument("--since")
    pg.add_argument("--tier", dest="tier_override", choices=["gold", "silver", "bronze"])
    pg.set_defaults(func=ingest.run)

    ps = sub.add_parser("synth")
    ps.add_argument("--vault", required=True)
    ps.add_argument("--provider")
    ps.add_argument("--limit", type=int)
    ps.add_argument("--since")
    ps.add_argument("--model-propose", dest="model_propose")
    ps.add_argument("--model-merge", dest="model_merge")
    ps.set_defaults(func=synth.run)

    pr = sub.add_parser("retrieve")
    pr.add_argument("--vault", required=True)
    pr.add_argument("--query")
    pr.set_defaults(func=retrieve.run)

    ph = sub.add_parser("hook")
    ph.add_argument("hook_action", choices=["install", "uninstall", "status"])
    ph.add_argument("--vault")
    ph.add_argument("--workspace", default=".")
    ph.set_defaults(func=hook.run)

    pgd = sub.add_parser("gardening")
    pgd.add_argument("--vault", required=True)
    pgd.add_argument("--threshold", type=float, default=0.4)
    pgd.add_argument("--dry-run", dest="dry_run", action="store_true")
    pgd.add_argument("--provider")
    pgd.add_argument("--model-merge", dest="model_merge")
    pgd.set_defaults(func=gardening.run)

    pc = sub.add_parser("config")
    pc.add_argument("action", choices=["get", "set"])
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_config_cmd)

    plog = sub.add_parser("log")
    plog.add_argument("--vault", required=True)
    plog.add_argument("--page")
    plog.add_argument("--limit", type=int, default=50)
    plog.set_defaults(func=_log_cmd)

    for name in ("search", "history", "diff", "revert", "tier"):
        sp = sub.add_parser(name)
        sp.set_defaults(func=_stub(name))

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
