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
description: The user's second brain (memex) — a local Markdown wiki compiled from past AI sessions (management, architecture, tech-leadership and coding), docs and code, plus per-workspace working memory. Use when the user refers to past work, decisions, people, teams or meetings ("como decidimos", "o que ficou daquela reunião", "quem é o dono de X", "já fizemos isso antes"), wants to continue where a previous session left off, asks you to remember something ("lembra disso", "salva isso"), asks to save the current state ("salva onde paramos"), or when you lack context about this project's or team's history that a past session might hold.
---

# memex — the second brain

A local-first Markdown brain covering the user's WHOLE work life — decisions,
people, teams, meetings, architecture, code. Hooks already inject context
automatically (session start = working memory; each prompt = relevant wiki
pages). This skill is for DELIBERATE use beyond that.

All commands work from any directory; the vault resolves automatically
(explicit `--vault <path>` overrides). If `memex` isn't on PATH, it lives at
`~/.local/bin/memex`.

## Find knowledge
1. `memex search "<terms>"` — scored pages with absolute file paths.
2. Read the page path for full detail; follow `[[wikilinks]]` with more searches.
3. Browse the vault directly: `index.md` (catalog), `SCHEMA.md` (conventions),
   `wiki/projects/<project>.md` (per-project/initiative hub), `now/<workspace>.md`
   (working memory for a workspace). Grep `wiki/` when search misses.

## Save knowledge (do this proactively)
- Durable fact, decision or preference worth keeping forever:
  `memex remember "<one clear, self-contained paragraph>"`
- Working state, when the user says "salva onde paramos" / before long pauses —
  write a SHORT Markdown handoff yourself (you know the session best) and pipe it:

  ```bash
  memex handoff --stdin <<'EOF'
  ## Contexto
  <what is being worked on and why — 1-3 sentences>
  ## Estado atual
  <what got done/decided this session; exact current state>
  ## Próximos passos
  - [ ] <next concrete action>
  ## Arquivos-chave
  - <path> — <why it matters now>
  EOF
  ```

  It overwrites `now/<workspace>.md` and is injected into this workspace's
  next session automatically.

## Rules
- Prefer `remember`/`handoff` over editing wiki pages directly. If you DO edit
  a page, follow `SCHEMA.md`: edit only the body (frontmatter is tool-owned),
  keep `[[wikilinks]]` valid, supersede decisions instead of deleting them.
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
        print("  Claude can now search/remember/handoff the brain in ANY workspace.")
        return 0
    if action == "uninstall":
        if uninstall():
            print("✓ memex skill removed.")
        else:
            print("memex skill was not installed.")
        return 0
    print(f"memex skill: {'installed' if installed() else 'not installed'} ({skill_dir() / 'SKILL.md'})")
    return 0
