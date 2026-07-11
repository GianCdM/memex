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

This vault is a **memex**: a local-first second brain compiled from the owner's
AI sessions — management, architecture, tech-leadership and coding alike — plus
documents and code (LLM-wiki model: raw sources → wiki → schema).
Any agent may READ everything here, and WRITE within the rules below.

## Layers (and what each one keys on)
- `raw/`    — episodic memory. One note per SESSION (a Claude conversation),
  verbatim and scrubbed. Immutable — never edit.
- `now/`    — working memory. One handoff page per WORKSPACE (the folder/repo
  a session runs in): "where we left off there". Overwritten freely; durable
  facts graduate to `wiki/` via synthesis.
- `wiki/`   — semantic memory. Pages carry a PROJECT (initiative/area/repo) in
  frontmatter: the git repo when the workspace is one, otherwise inferred from
  the CONTENT — a management session run from a generic folder still lands in
  the right initiative. Many sessions and workspaces feed one project.
- `index.md` — catalog of every wiki page (regenerated on each synthesis).
- `log.md`  — append-only chronology of what changed the brain.
- `.memex/` — tool state (indexes, ledgers, locks). Machine-owned; hands off.

## Wiki sections
- `wiki/topics/`    — concepts, processes, strategies, how-tos, domain knowledge.
- `wiki/entities/`  — people, teams, services, systems, vendors (one page each).
- `wiki/decisions/` — decisions, organizational or technical, ADR-style:
  Context / Decision / Consequences. Never delete a decision — supersede it
  (add `status: superseded` and a [[wikilink]] to the newer decision).
- `wiki/projects/`  — one hub per project tying sessions + docs + architecture.

## Page format
- YAML frontmatter (`title`, `tags`, `tier`, `project`, `sources`, `updated`)
  is tool-owned — edit the body, leave the frontmatter to memex.
- Body: Markdown, `##` headings, [[wikilinks]] between related pages.
- Reference code by repo path (`repo/src/file.py`) — never paste files that
  live in git.

## Store vs skip
STORE: decisions + their rationale (org or technical) · action items and
commitments (who/what/when) · facts about people, teams and systems (ownership,
stakeholders) · outcomes of meetings and 1:1s (conclusions, not minutes) ·
invariants and constraints · non-obvious fixes and recurring patterns · user
preferences and corrections · milestones. SKIP: transient debugging, dead ends,
code that lives in git, secrets (always scrubbed at capture), one-off trivia.

## Trust tiers
bronze = raw captures · silver = session/doc pages (edit freely) · gold =
curated/code pages (edit deliberately; prior versions snapshot to
`.memex/history/`).

## Maintenance is automatic
Synthesis (raw → wiki), the now-page refresh, and near-duplicate consolidation
("tidy", recoverable — absorbed pages archive to `.memex/history/gardening/`)
all run in the background after sessions end. Below-threshold overlaps surface
in `wiki/_sugestoes.md` for a human call. Nothing to remember.

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
