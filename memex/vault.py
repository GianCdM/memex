"""memex vault — scaffold and manage vaults (the brain data store)."""

from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA_TEMPLATE = """# memex vault schema

Conventions the LLM follows when maintaining this wiki (LLM-Wiki / Karpathy model).

## Layers
- `raw/`    — verbatim sources (forensic, immutable). Never edited by the LLM.
- `wiki/`   — synthesized knowledge (what you read). Organized by topic.
- `.memex/` — tool index and state (gitignored).

## Trust tiers (medallion, by source)
- **silver** — pages from sessions/docs: the LLM edits directly.
- **gold**   — pages from code/curated sources: the LLM plans + audits before editing.
A page mixing sources takes the highest tier (Max).

## Wiki layout
- `wiki/topics/`    — subjects / concepts
- `wiki/entities/`  — people, services, projects
- `wiki/decisions/` — decision records

Every page cites its sources in frontmatter (`sources:`) and links with [[wikilinks]].
"""

INDEX_TEMPLATE = """# Brain index

Navigable catalog of wiki pages, by category.
_(generated/updated by `memex synth`)_

## Topics

## Entities

## Decisions
"""

# The vault's own .gitignore (git is optional, but if used, keep tool state out).
VAULT_GITIGNORE = ".memex/\n"


def new(args) -> int:
    path = Path(os.path.expanduser(args.path)).resolve()

    if path.exists() and any(path.iterdir()):
        print(f"error: {path} already exists and is not empty.")
        return 1

    # structure
    (path / "raw").mkdir(parents=True, exist_ok=True)
    for section in ("topics", "entities", "decisions"):
        (path / "wiki" / section).mkdir(parents=True, exist_ok=True)
    memex_dir = path / ".memex"
    (memex_dir / "history").mkdir(parents=True, exist_ok=True)

    # files
    (path / "schema.md").write_text(SCHEMA_TEMPLATE)
    (path / "index.md").write_text(INDEX_TEMPLATE)
    (path / ".gitignore").write_text(VAULT_GITIGNORE)

    config = {
        "vault_version": 1,
        "default_tier": args.tier,
        "provider": {"order": ["claude", "ollama"]},
        "models": {"propose": None, "merge": None},
    }
    (memex_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (memex_dir / "index.json").write_text(json.dumps({"pages": []}, indent=2) + "\n")
    for ledger in ("ledger.jsonl", "changelog.jsonl", "metrics.jsonl"):
        (memex_dir / ledger).touch()

    print(f"✓ vault created at {path}")
    print(f"  default tier: {args.tier}")
    print("  structure:   raw/  wiki/{topics,entities,decisions}/  .memex/")
    print()
    print("Next steps:")
    print(f"  - open in Obsidian: point a vault at {path}")
    print("  - git is optional: run `git init` there if you want versioning")
    print(f"  - in a workspace:   memex init --vault {path}")
    return 0
