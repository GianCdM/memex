"""memex init — onboard a workspace into a vault (orchestrator).

Run inside a workspace. Registers workspace -> vault, then backfills this
workspace's sessions + codebase into the vault. Optionally synthesizes.
Continuous-capture hooks are on the roadmap.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from . import config as config_mod
from . import ingest
from . import synth
from . import vault as vault_mod


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"vault {vault} does not exist — creating it.")
        vault_mod.new(Namespace(path=str(vault), tier="personal"))
        print()

    workspace = str(Path(getattr(args, "workspace", ".") or ".").expanduser().resolve())
    print(f"init: workspace {workspace}  ->  vault {vault}\n")

    g = config_mod.load_global()
    g.setdefault("workspaces", {})[workspace] = str(vault)
    config_mod.save_global(g)
    print(f"registered workspace -> vault in {config_mod.global_config_path()}\n")

    ingest.run(Namespace(
        vault=str(vault), all=True, workspace=workspace,
        codebase=(workspace if getattr(args, "codebase", True) else None),
        doc=None, source="auto", since=getattr(args, "since", None),
        tier_override=None, session=None,
    ))

    if getattr(args, "synth", False):
        print()
        synth.run(Namespace(
            vault=str(vault), provider=getattr(args, "provider", None),
            limit=getattr(args, "limit", None), since=None,
            model_propose=None, model_merge=None,
        ))
    else:
        print(f"\n(skipped synth — run `memex synth --vault {vault}` when ready)")

    print("\nnote: continuous-capture hooks are on the roadmap (not wired yet).")
    return 0
