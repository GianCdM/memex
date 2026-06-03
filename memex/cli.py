"""memex CLI — command router (argparse, stdlib only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import doctor
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
    )
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("doctor", help="detect environment + recommend provider/model setup")
    p.set_defaults(func=doctor.run)

    pv = sub.add_parser("vault", help="manage vaults (the brain data store)")
    pv.set_defaults(func=lambda a: (pv.print_help() or 0))
    vs = pv.add_subparsers(dest="vault_command", metavar="<subcommand>")
    pvn = vs.add_parser("new", help="scaffold a clean vault")
    pvn.add_argument("path")
    pvn.add_argument("--tier", choices=["personal", "work"], default="personal")
    pvn.set_defaults(func=vault.new)

    pi = sub.add_parser("init", help="(workspace) onboard: backfill this workspace into a vault")
    pi.add_argument("--vault", required=True)
    pi.add_argument("--workspace", default=".")
    pi.add_argument("--since")
    pi.add_argument("--no-codebase", dest="codebase", action="store_false")
    pi.add_argument("--synth", action="store_true", help="also build the wiki now")
    pi.add_argument("--provider")
    pi.add_argument("--limit", type=int)
    pi.set_defaults(func=init_mod.run)

    pg = sub.add_parser("ingest", help="capture sessions/codebase/docs into raw/")
    pg.add_argument("--vault", required=True)
    pg.add_argument("--all", action="store_true", help="backfill sessions")
    pg.add_argument("--codebase", nargs="?", const=".", default=None, metavar="PATH")
    pg.add_argument("--doc", metavar="FILE")
    pg.add_argument("--source", choices=["auto", "claude", "cursor", "codex"], default="auto")
    pg.add_argument("--workspace", help="scope sessions to this workspace path")
    pg.add_argument("--since", help="only sessions/files on/after YYYY-MM-DD")
    pg.add_argument("--tier", dest="tier_override", choices=["gold", "silver", "bronze"])
    pg.set_defaults(func=ingest.run)

    ps = sub.add_parser("synth", help="compile raw/ into the wiki/ (LLM step)")
    ps.add_argument("--vault", required=True)
    ps.add_argument("--provider", help="claude | ollama | openai | ... (overrides config order)")
    ps.add_argument("--limit", type=int, help="cap raw notes processed this run")
    ps.add_argument("--since", help="only raw notes on/after YYYY-MM-DD")
    ps.add_argument("--model-propose", dest="model_propose")
    ps.add_argument("--model-merge", dest="model_merge")
    ps.set_defaults(func=synth.run)

    pr = sub.add_parser("retrieve", help="UserPromptSubmit hook: inject relevant wiki pages")
    pr.add_argument("--vault", required=True)
    pr.add_argument("--query", help="(testing) use this text instead of reading the prompt from stdin")
    pr.set_defaults(func=retrieve.run)

    ph = sub.add_parser("hook", help="install/uninstall/status the capture+recall hooks (per workspace)")
    ph.add_argument("hook_action", choices=["install", "uninstall", "status"])
    ph.add_argument("--vault", help="vault to point the hooks at (required for install)")
    ph.add_argument("--workspace", default=".", help="workspace to wire (default: current dir)")
    ph.set_defaults(func=hook.run)

    pc = sub.add_parser("config", help="get/set global config")
    pc.add_argument("action", choices=["get", "set"])
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_config_cmd)

    pstat = sub.add_parser("status", help="ingest/synth health for a vault")
    pstat.add_argument("--vault", required=True)
    pstat.set_defaults(func=_status_cmd)

    plog = sub.add_parser("log", help="list executed wiki edits (changelog)")
    plog.add_argument("--vault", required=True)
    plog.add_argument("--page")
    plog.add_argument("--limit", type=int, default=50)
    plog.set_defaults(func=_log_cmd)

    for name, help_ in [
        ("search", "search the wiki from the CLI"),
        ("history", "version timeline of a page"),
        ("diff", "show what a change did"),
        ("revert", "restore a page to a past version"),
        ("tier", "re-classify a page's tier"),
        ("gardening", "merge duplicates / prune"),
    ]:
        sp = sub.add_parser(name, help=help_)
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
