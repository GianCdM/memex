"""memex vault — scaffold, upgrade and manage vaults (the brain data store).

`ensure()` is idempotent: it creates whatever is missing, so it both scaffolds
a fresh vault and upgrades a v1 vault in place (adds now/, log.md, SCHEMA.md)
without touching user content.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# SCHEMA.md is the contract (Karpathy's LLM-wiki model: sources → wiki → schema).
# It is written for AGENTS as much as for humans: any agent that can read this
# file knows how to read from and write to the brain.
SCHEMA_TEMPLATE = """# SCHEMA — how this brain is organized

This vault is a **memex**: a local-first second brain compiled from AI coding
sessions, documents and code (LLM-wiki model: raw sources → wiki → schema).
Any agent may READ everything here, and WRITE within the rules below.

## Layers
- `raw/`    — verbatim captures (sessions, docs). Immutable — never edit.
- `wiki/`   — synthesized knowledge (what you read). Edit following the rules.
- `now/`    — working memory: one handoff page per project ("where we left
  off"). Overwritten freely; durable facts graduate to `wiki/` via synthesis.
- `index.md` — catalog of every wiki page (regenerated on each synthesis).
- `log.md`  — append-only chronology of what changed the brain.
- `.memex/` — tool state (indexes, ledgers, locks). Machine-owned; hands off.

## Wiki sections
- `wiki/topics/`    — concepts, how-tos, domain knowledge.
- `wiki/entities/`  — people, teams, services, systems, tools.
- `wiki/decisions/` — decisions, ADR-style: Context / Decision / Consequences.
  Never delete a decision — supersede it (add `status: superseded` and a
  [[wikilink]] to the newer decision).
- `wiki/projects/`  — one hub per project tying sessions + docs + architecture.

## Page format
- YAML frontmatter (`title`, `tags`, `tier`, `project`, `sources`, `updated`)
  is tool-owned — edit the body, leave the frontmatter to memex.
- Body: Markdown, `##` headings, [[wikilinks]] between related pages.
- Reference code by repo path (`repo/src/file.py`) — never paste files that
  live in git.

## Store vs skip
STORE: decisions + their rationale · invariants and constraints · non-obvious
fixes and recurring bug patterns · user preferences and corrections · project
milestones. SKIP: transient debugging, dead ends, code that lives in git,
secrets (always scrubbed at capture), one-off trivia.

## Trust tiers
bronze = raw captures · silver = session/doc pages (edit freely) · gold =
curated/code pages (edit deliberately; prior versions snapshot to
`.memex/history/`).

## How agents use this brain
- Find:  `memex search "<terms>"` → scored pages with file paths; Read them.
  Or browse `index.md` (catalog) and follow [[wikilinks]].
- Save a durable fact NOW: `memex remember "<one clear paragraph>"`.
- Save working state ("where we left off"): pipe a short Markdown handoff to
  `memex handoff --stdin` (sections: Contexto / Estado atual / Próximos
  passos / Arquivos-chave).
- Everything else is automatic: capture on session end, recall on prompt,
  boot on session start.
"""

INDEX_TEMPLATE = """# Brain index

Navigable catalog of wiki pages, by category.
_(generated/updated by `memex synth`)_

## Topics

## Entities

## Decisions
"""

LOG_TEMPLATE = """# Brain log

Append-only chronology of what changed this brain (newest last).
_(written by memex — reflect/synth/gardening/handoff runs)_
"""

# The vault's own .gitignore (git is optional, but if used, keep tool state out).
VAULT_GITIGNORE = ".memex/\n"

_V1_SCHEMA_MARKER = "# memex vault schema"

DEFAULT_CONFIG = {
    "vault_version": 2,
    "default_tier": "personal",
    "provider": {"order": ["claude", "ollama"]},
    "models": {"propose": None, "merge": None},
}


def ensure(path, tier="personal", quiet=False) -> bool:
    """Create-or-upgrade a vault in place, idempotently. Returns True if it
    created/changed anything. Never touches user content (wiki/, raw/, now/)."""
    path = Path(path).expanduser().resolve()
    changed = False

    for d in ("raw", "now", "wiki/topics", "wiki/entities", "wiki/decisions",
              "wiki/projects"):
        p = path / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            changed = True
    memex_dir = path / ".memex"
    for d in ("history", "state"):
        p = memex_dir / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            changed = True

    schema = path / "SCHEMA.md"
    legacy = path / "schema.md"
    # v1 shipped a lowercase, tool-focused schema.md. It was never meant to be
    # user-edited, so replace it with the v2 agent contract; on case-insensitive
    # filesystems SCHEMA.md/schema.md are one file, so just rewrite in place.
    if schema.exists() or legacy.exists():
        existing = (schema if schema.exists() else legacy)
        try:
            is_v1 = existing.read_text(encoding="utf-8", errors="ignore").startswith(_V1_SCHEMA_MARKER)
        except OSError:
            is_v1 = False
        if is_v1:
            if legacy.exists() and not _same_file(legacy, schema):
                legacy.unlink()
            schema.write_text(SCHEMA_TEMPLATE, encoding="utf-8")
            changed = True
        elif legacy.exists() and not _same_file(legacy, schema):
            os.replace(legacy, schema)  # keep user content, normalize the name
            changed = True
    else:
        schema.write_text(SCHEMA_TEMPLATE, encoding="utf-8")
        changed = True

    if not (path / "index.md").exists():
        (path / "index.md").write_text(INDEX_TEMPLATE, encoding="utf-8")
        changed = True
    if not (path / "log.md").exists():
        (path / "log.md").write_text(LOG_TEMPLATE, encoding="utf-8")
        changed = True
    if not (path / ".gitignore").exists():
        (path / ".gitignore").write_text(VAULT_GITIGNORE, encoding="utf-8")
        changed = True

    cfg_path = memex_dir / "config.json"
    if not cfg_path.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["default_tier"] = tier
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        changed = True
    if not (memex_dir / "index.json").exists():
        (memex_dir / "index.json").write_text(json.dumps({"pages": []}, indent=2) + "\n",
                                              encoding="utf-8")
        changed = True
    for ledger in ("ledger.jsonl", "changelog.jsonl", "metrics.jsonl"):
        p = memex_dir / ledger
        if not p.exists():
            p.touch()
            changed = True

    if changed and not quiet:
        print(f"✓ vault ready at {path}")
    return changed


def _same_file(a: Path, b: Path) -> bool:
    """True when two paths point at the same file (case-insensitive FS)."""
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def log_append(vault, entry: str) -> None:
    """One line into the vault's human-readable log.md (append-only). Best-effort."""
    from datetime import datetime, timezone
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with (Path(vault) / "log.md").open("a", encoding="utf-8") as f:
            f.write(f"- `[{ts}]` {entry}\n")
    except Exception:
        pass


def new(args) -> int:
    path = Path(os.path.expanduser(args.path)).resolve()

    if path.exists() and any(path.iterdir()):
        print(f"error: {path} already exists and is not empty.")
        return 1

    ensure(path, tier=args.tier, quiet=True)

    print(f"✓ vault created at {path}")
    print(f"  default tier: {args.tier}")
    print("  structure:   raw/  now/  wiki/{topics,entities,decisions,projects}/  .memex/")
    print()
    print("Next steps:")
    print(f"  - open in Obsidian: point a vault at {path}")
    print("  - git is optional: run `git init` there if you want versioning")
    print(f"  - in a workspace:   memex init --vault {path}")
    return 0
