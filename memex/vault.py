"""memex vault — scaffold, upgrade and manage vaults (the brain data store).

`ensure()` is idempotent: it creates whatever is missing, so it both scaffolds
a fresh vault and upgrades a v1 vault in place (adds workspace/, log.md, SCHEMA.md)
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
- `ABOUT.md` — the OWNER's profile (role, focus, language, teams). Human-edited;
  the synthesizer reads it to judge what is durable *for this person*.
- `raw/`    — episodic memory. One note per SESSION (a Claude conversation),
  verbatim and scrubbed. Immutable — never edit. It is a forensic source,
  not the default boot context.
- `workspace/` — working memory. One handoff page per WORKSPACE (the Git root
  or non-Git folder a session runs in): "where we left off there". Its filename
  is a collision-safe, home-relative path key (for example `src-acme-repos-api`),
  while its frontmatter preserves the short display name and root path. Overwritten
  freely; durable facts graduate to `wiki/` via synthesis.
- `wiki/` — current canonical semantic memory only: topics, entities, decisions.
  Pages carry a PROJECT (initiative/area/repo) in frontmatter: the git repo when
  the workspace is one, otherwise inferred from the CONTENT — a management
  session run from a generic folder still lands in the right initiative. Many
  sessions and workspaces feed one project.
- `log.md`  — append-only chronology of what changed the brain.
- `.memex/` — tool state (indexes, ledgers, locks). Machine-owned; hands off.
  - `.memex/views/` — regenerated catalogs and project navigation, not knowledge.
  - `.memex/audit/` — health reports and duplicate candidates, not knowledge.
  - `.memex/history/` — machine-managed prior page revisions, outside normal
    recall and graph.

## Wiki sections
- `wiki/topics/`    — concepts, processes, strategies, how-tos, domain knowledge.
- `wiki/entities/`  — people, teams, services, systems, vendors (one page each).
- `wiki/decisions/` — decisions, organizational or technical, ADR-style:
  Context / Decision / Consequences. Never delete a decision — supersede it
  (add `status: superseded` and a `[[wikilink]]` to the newer decision).

## Page format
- YAML frontmatter (`title`, `tags`, `kind`, `status`, `superseded_by`,
  `project`, `sources`, `updated`) is tool-owned — edit the body, leave
  the frontmatter to memex.
- Body: Markdown, `##` headings, `[[wikilinks]]` between related pages.
- Reference code by repo path (`repo/src/file.py`) — never paste files that
  live in git.

## Store vs skip
STORE: decisions + their rationale (org or technical) · action items and
commitments (who/what/when) · facts about people, teams and systems (ownership,
stakeholders) · outcomes of meetings and 1:1s (conclusions, not minutes) ·
invariants and constraints · non-obvious fixes and recurring patterns · user
preferences and corrections · milestones. SKIP: transient debugging, dead ends,
code that lives in git, secrets (always scrubbed at capture), one-off trivia.

## Page metadata
- `kind` — where this page came from: `session` (AI session), `doc` (imported
  document), `manual` (`memex remember`), `code` (repo analysis), `merged`
  (auto-consolidated near-duplicates). Informational only — no behavior.
- `status` — whether this page still holds: `current` (default), `superseded`
  (replaced — `superseded_by` link required), `obsolete` (project dead),
  `deprecated` (still useful, recommendation changed), `archived` (correct but
  dormant), `draft` (incomplete). Edit by hand or let the LLM propose.
- `## 📋 Histórico` — auto-maintained changelog at the bottom of each page
  (≤10 entries, one line per merge with a link to the raw source).

## Maintenance is automatic
Synthesis (raw → wiki), the workspace-page refresh, and near-duplicate consolidation
("tidy", recoverable — absorbed pages archive to `.memex/history/gardening/`)
all run in the background after sessions end. Below-threshold overlaps surface
in `.memex/audit/merge-suggestions.md` for a human call. Nothing to remember.

## How agents use this brain
- Find:  `memex search "<terms>"` → scored pages with file paths; Read them.
  Or browse `.memex/views/brain-index.md` (catalog) and follow `[[wikilinks]]`.
- Save a durable fact NOW: `memex remember "<one clear paragraph>"`.
- The `workspace/` page (written automatically by reflect after each session) is the
  primary boot context — "where we left off" for the next session.
- `raw/` remains available for forensic detail. If `limits.boot_raw_tail_chars`
  is greater than zero, boot may inject a bounded tail only when the workspace-page
  is missing, stale, or behind the latest capture; it never injects the full raw.
- Everything else is automatic: capture on session end, recall on prompt,
  boot on session start.
"""

# The owner's profile — read by the synthesizer to decide what matters. The
# template is deliberately useful even unedited, but the whole point is that
# YOU edit it (nothing about the owner is hardcoded in memex itself).
ABOUT_TEMPLATE = """# ABOUT — who owns this brain

> Edit freely. The synthesizer reads this file to judge what knowledge is
> durable **for you** — your role, your focus, your language. Keep it short.

- **Role:** (e.g. engineering manager · architect · tech lead · engineer)
- **Day-to-day:** (e.g. management, architecture reviews, 1:1s, sometimes coding)
- **Cares about:** (e.g. decisions & their rationale, team commitments, system ownership)
- **Language:** (e.g. Portuguese for notes, English for code)
- **Teams / areas:** (e.g. checkout, payments)
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
_(written by memex — reflect/synth/gardening runs)_
"""

# The vault's own .gitignore (git is optional, but if used, keep tool state out).
VAULT_GITIGNORE = ".memex/\n"

_V1_SCHEMA_MARKER = "# memex vault schema"

# Per-vault config: only keys something actually READS live here — "models"
# overrides the provider's models for this vault (config.resolve_provider) and
# a "limits" block overrides limits.py knobs. Provider ORDER is global-only.
DEFAULT_CONFIG = {
    "vault_version": 2,
    "models": {"propose": None, "merge": None},
}


def _schema_needs_refresh(path):
    """Refresh only the shipped legacy schema, never a user-authored one."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return (text.startswith(_V1_SCHEMA_MARKER)
            or "## Trust tiers" in text
            or "tier: " in text
            or "repo name, else folder name" in text)


def ensure(path, quiet=False) -> bool:
    """Create-or-upgrade a vault in place, idempotently. Returns True if it
    created/changed anything. Never touches user content (wiki/, raw/, workspace/)."""
    path = Path(path).expanduser().resolve()
    changed = False

    for d in ("raw", "workspace", "wiki/topics", "wiki/entities", "wiki/decisions"):
        p = path / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            changed = True

    # v1→v2 migration: rename now/ → workspace/
    old_now = path / "now"
    new_workspace = path / "workspace"
    if old_now.is_dir() and not new_workspace.is_dir():
        old_now.rename(new_workspace)
        changed = True

    memex_dir = path / ".memex"
    for d in ("history", "state", "review/pending", "review/applying", "review/applied",
              "review/rejected", "review/stale", "audit", "views/projects", "manifests"):
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
        if is_v1 or _schema_needs_refresh(existing):
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

    if not (path / "ABOUT.md").exists():
        (path / "ABOUT.md").write_text(ABOUT_TEMPLATE, encoding="utf-8")
        changed = True
    # The brain catalog is now a regenerated view (.memex/views/), not a wiki
    # page — seed a minimal placeholder so a fresh vault still has one to read.
    views_dir = memex_dir / "views"
    if not (views_dir / "brain-index.md").exists():
        views_dir.mkdir(parents=True, exist_ok=True)
        (views_dir / "brain-index.md").write_text(INDEX_TEMPLATE, encoding="utf-8")
        changed = True
    if not (path / "log.md").exists():
        (path / "log.md").write_text(LOG_TEMPLATE, encoding="utf-8")
        changed = True
    if not (path / ".gitignore").exists():
        (path / ".gitignore").write_text(VAULT_GITIGNORE, encoding="utf-8")
        changed = True

    cfg_path = memex_dir / "config.json"
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
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

    ensure(path, quiet=True)

    print(f"✓ vault created at {path}")
    print("  structure:   raw/  workspace/  wiki/{topics,entities,decisions}/  .memex/{views,audit,history}/")
    print()
    print("Next steps:")
    print(f"  - open in Obsidian: point a vault at {path}")
    print("  - git is optional: run `git init` there if you want versioning")
    print(f"  - in a workspace:   memex init --vault {path}")
    return 0
