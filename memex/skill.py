"""memex skill — install the user-level Claude Code skill.

The hooks make the brain automatic; the SKILL makes Claude a deliberate reader
and writer of it. It's installed at USER level (~/.claude/skills/memex/) so it
works in every workspace on the machine, and it's model-invocable: Claude
reaches for it when you refer to past work ("como decidimos...", "continua de
onde paramos", "lembra disso") — no slash command needed, though /memex works.

Every command in the skill resolves the vault itself (via memex's config), so
the skill file has no machine-specific paths and never goes stale.
"""

from __future__ import annotations

from pathlib import Path

SKILL_DIRNAME = "memex"

SKILL_TEMPLATE = """---
name: memex
description: The user's second brain (memex) — a local Markdown wiki compiled from past AI sessions (management, architecture, tech-leadership and coding), docs and code, plus per-workspace working memory. Use when the user refers to past work, decisions, people, teams or meetings ("como decidimos", "o que ficou daquela reunião", "quem é o dono de X", "já fizemos isso antes"), wants to continue where a previous session left off, asks you to remember something ("lembra disso", "salva isso"), or when you lack context about this project's or team's history that a past session might hold.
---

# memex — the second brain

A local-first Markdown brain covering the user's WHOLE work life — decisions,
people, teams, meetings, architecture, code. Hooks already inject context
automatically (session start = working memory; each prompt = relevant wiki
pages). This skill is for DELIBERATE use beyond that.

## MCP tools — use these, not Bash commands

The memex MCP server exposes five tools. Prefer these over running `memex` via
Bash — they return structured data and avoid shell escaping issues.

### search
Find pages in the brain. Call this when the user asks about past decisions,
people, projects, or any knowledge the brain might hold.

- `query` (required): keywords, not natural language. Use the user's own words.
- `limit` (optional): max results, default 5.

Returns scored pages with file paths. Escalate deliberately: `page` for a
budgeted body read, `timeline` to reconstruct the flow around a hit;
follow `[[wikilinks]]` with more searches.

### timeline
The ordered compilation trail of one page (or one raw capture) — every applied
merge over time. Use it to reconstruct the decision/debugging flow around a
point before paying for full page reads.

- `slug` OR `raw` (required): page slug, or a raw capture filename.
- `limit` (optional): max events returned, most recent kept (default 20).

### page
Read one canonical page's body under a character budget — escalate from a
search hit without paying for the whole file.

- `slug` OR `path` (required).
- `max_chars` (optional): body budget (default from limits, 8000).
- `mode` (optional): `head` (default) or `tail` — which slice when the body
  exceeds the budget. The response reports total size and how to get the rest.

### remember
File one durable fact, decision, or preference into the brain.

- `text` (required): one clear, self-contained paragraph. Include rationale.

The note is ingested into raw/ immediately and returns at once; it is compiled
into a wiki page on the next reflect (batched, parallel, detached). To force
compilation now, run `memex reflect`.

### status
Peek at the brain — how many raw notes, wiki pages, pending synthesis,
workspace-pages, and suggestions. Use when the user asks about their brain's state.

## Fallback: CLI

If the MCP server is not configured, the same functionality is available via:

- `memex search "<terms>"` — find pages (Bash, parse stdout)
- `memex timeline <slug>` — the compilation trail of a page
- `memex page <slug> [--chars N] [--tail]` — budgeted body read
- Browse the vault: `.memex/views/brain-index.md` (catalog), `SCHEMA.md`
  (conventions), `.memex/views/projects/<project>.md` (per-project hub),
  `workspace/<workspace>.md` (working memory). Grep `wiki/` when search misses.

## Rules
- Save durable knowledge proactively via the `remember` tool. Better to save
  and later consolidate than to lose a fact.
- If you DO edit a wiki page, follow `SCHEMA.md`: edit only the body
  (frontmatter is tool-owned), keep `[[wikilinks]]` valid, supersede decisions
  instead of deleting them.
- The vault's `ABOUT.md` is the owner's profile (role, focus, language) — the
  synthesizer reads it to judge what matters. If the user tells you something
  durable about themselves ("sou gestor de X agora", "meu foco mudou para Y"),
  offer to update `ABOUT.md` accordingly.
- `raw/` is immutable — never write there. `.memex/` is machine state — hands off.
- Never store secrets in the brain (captures are scrubbed; keep it that way).
"""


def skill_dir(base=None) -> Path:
    return Path(base or Path.home() / ".claude" / "skills") / SKILL_DIRNAME


def install(base=None) -> Path:
    d = skill_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL_TEMPLATE, encoding="utf-8")
    return d / "SKILL.md"


def uninstall(base=None) -> bool:
    f = skill_dir(base) / "SKILL.md"
    if f.exists():
        f.unlink()
        try:
            f.parent.rmdir()
        except OSError:
            pass
        return True
    return False


def installed(base=None) -> bool:
    return (skill_dir(base) / "SKILL.md").exists()


def run(args) -> int:
    action = getattr(args, "skill_action", "install")
    if action == "install":
        f = install()
        print(f"✓ memex skill installed (user-level): {f}")
        print("  Claude can now search/remember the brain in ANY workspace via MCP tools.")
        return 0
    if action == "uninstall":
        if uninstall():
            print("✓ memex skill removed.")
        else:
            print("memex skill was not installed.")
        return 0
    print(f"memex skill: {'installed' if installed() else 'not installed'} ({skill_dir() / 'SKILL.md'})")
    return 0
