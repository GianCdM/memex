"""memex CLI — command router (argparse, stdlib only)."""

from __future__ import annotations

import argparse
import sys

from . import __version__, doctor, vault


def _stub(name: str):
    """Placeholder for commands that are on the roadmap but not built yet."""

    def run(args):
        print(f"memex {name}: planned, not implemented yet.")
        print("See the roadmap in README.md.")
        return 0

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memex",
        description="A portable, local-first second brain built from your AI coding sessions.",
    )
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # --- doctor (working) ---
    p_doctor = sub.add_parser("doctor", help="detect environment + recommend provider/model setup")
    p_doctor.set_defaults(func=doctor.run)

    # --- vault (working: `vault new`) ---
    p_vault = sub.add_parser("vault", help="manage vaults (the brain data store)")
    p_vault.set_defaults(func=lambda a: (p_vault.print_help() or 0))
    v_sub = p_vault.add_subparsers(dest="vault_command", metavar="<subcommand>")
    p_vault_new = v_sub.add_parser("new", help="scaffold a clean vault")
    p_vault_new.add_argument("path", help="path of the new vault, e.g. ~/vaults/personal")
    p_vault_new.add_argument(
        "--tier",
        choices=["personal", "work"],
        default="personal",
        help="default tier of this vault (default: personal)",
    )
    p_vault_new.set_defaults(func=vault.new)

    # --- roadmap stubs (declared so `--help` shows the real surface) ---
    roadmap = [
        ("init", "(workspace) opt-in: wire hooks + link workspace to a vault"),
        ("ingest", "capture sessions/code/docs into raw/"),
        ("synth", "compile raw/ into the wiki/ (LLM step)"),
        ("retrieve", "UserPromptSubmit hook: inject relevant wiki pages"),
        ("hook", "install/uninstall/status of capture hooks"),
        ("search", "search the wiki from the CLI"),
        ("status", "ingest health + pending work"),
        ("log", "list executed gold edits"),
        ("history", "version timeline of a page"),
        ("diff", "show what a change did"),
        ("revert", "restore a page to a past version"),
        ("tier", "re-classify a page's tier"),
        ("gardening", "merge duplicates / prune"),
        ("config", "get/set config (provider, models, ...)"),
    ]
    for name, help_ in roadmap:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=_stub(name))

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
