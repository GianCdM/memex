# memex

> Your second brain for AI sessions — decisions, architecture, people, projects, and code, turned into a searchable Markdown wiki and working memory that picks up where you left off.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What it is

`memex` wires into your AI tools (Claude Code, Cursor, Codex) and turns every session into durable, navigable knowledge — without leaving your machine.

- **Long-term memory:** a Markdown wiki (`wiki/`) with decisions, entities, projects, and facts. Opens in Obsidian, searchable from the terminal and by the AI.
- **Working memory:** a per-workspace now-page (`now/<workspace>.md`) injected at session start — "where we left off," no re-explaining.
- **Episodic memory:** immutable, LLM-free raw transcripts (`raw/`) kept as forensic source material.

Deliberate `handoff` > auto-synthesis > raw tail fallback. The good version wins.

Inspired by Vannevar Bush's *memex* (1945) and Karpathy's [LLM-Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

---

## Install

```bash
# uv (brings its own Python)
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
winget install astral-sh.uv                         # Windows

# memex
uv tool install git+https://github.com/GianCdM/memex.git
uv tool update-shell

# document extraction (recommended)
uv tool install 'markitdown[all]'
# optional: tesseract (OCR), openai-whisper (audio) — both local, no cloud

# verify
memex doctor
```

The `claude` provider needs the Claude Code CLI logged in once (`claude` → `/login`). Alternative: any OpenAI-compatible endpoint (Ollama runs free and local).

Upgrade: `uv tool upgrade memex`.

---

## Quickstart

```bash
memex doctor          # check setup
memex init            # activate this workspace (vault + hooks + skill + backfill)
                      # repeat in each workspace you work in

memex search "dedup pipeline"           # talk to the brain
memex remember "We decided X because Y."  # save a fact
memex handoff --show                    # where we left off
memex briefing --show                   # today's agenda
memex                                   # status: what's in your brain
```

`init` is a deliberate, per-workspace opt-in. It backfills every old session found for that workspace.

Useful flags: `--vault <path>` (separate brain for personal/work) · `--no-analyze` (skip code hubs) · `--no-docs` · `--docs-from <path>` · `--index <jsonl>` · `--index-mcp`.

---

## How it works

```mermaid
flowchart TB
  classDef you fill:#16a34a,color:#fff,stroke:#15803d,stroke-width:3px
  classDef hook fill:#0ea5e9,color:#fff,stroke:#0369a1,stroke-width:2px
  classDef llm fill:#7c3aed,color:#fff,stroke:#5b21b6,stroke-width:2px
  classDef bronze fill:#cd7f32,color:#fff,stroke:#8b5a2b,stroke-width:2px
  classDef wiki fill:#cbd5e1,color:#000,stroke:#475569,stroke-width:3px
  classDef nowc fill:#f59e0b,color:#000,stroke:#b45309,stroke-width:2px

  YOU["YOU — run once<br/><b>memex init</b><br/>hooks + skill + first capture"]:::you

  subgraph AUTO["then memex runs itself"]
    direction TB
    HARNESS["Claude Code session"]
    BOOT(["hook · SessionStart → <b>boot</b>"]):::hook
    RECALL(["hook · UserPromptSubmit → <b>recall</b>"]):::hook
    CAP(["hooks · SessionEnd + PreCompact → <b>capture</b>"]):::hook
    RAW["raw/ — episodic memory<br/>immutable, scrubbed, LLM-free"]:::bronze
    REFL["<b>reflect</b> — detached, the only LLM stage<br/>synth (raw→wiki) + now-page"]:::llm
    WIKI["wiki/ — long-term memory<br/>topics · entities · decisions · projects"]:::wiki
    NOW["now/&lt;workspace&gt;.md — working memory<br/>'where we left off'"]:::nowc

    BOOT -- "inject now-page" --> HARNESS
    RECALL -- "inject wiki pages (deduped)" --> HARNESS
    HARNESS --> CAP --> RAW --> REFL
    REFL --> WIKI
    REFL --> NOW
    NOW -.-> BOOT
    WIKI -.-> RECALL
  end

  SKILL["skill · ~/.claude/skills/memex<br/>search · remember · handoff — Claude as a deliberate writer"]:::llm
  YOU --> HARNESS
  SKILL -.-> HARNESS
```

Four hooks close the loop. `boot` injects working memory at session start. `recall` injects relevant wiki pages per prompt. `capture` saves every session (even pre-compaction). `reflect` runs detached in the background — the only step that uses an LLM.

A user-level skill teaches the AI to search the wiki, file facts, and write handoffs deliberately. The wiki isn't a side artifact; it's a read/write surface for the agent.

---

## Memory layers

| Layer | Where | Lifetime | Written by |
|---|---|---|---|
| **Episodic** | `raw/` | forever, immutable | `capture` (LLM-free) |
| **Working** | `now/<workspace>.md` | current effort, overwritten | `handoff` (deliberate) or `reflect` (auto) |
| **Semantic** | `wiki/` | forever, curated | `reflect` / `synth` |

---

## Session → workspace → project

| Claude concept | memex layer | Keyed by |
|---|---|---|
| one **session** | `raw/` | session id |
| the **workspace** it ran in | `now/` | repo name, else folder name |
| the **project/initiative** | `wiki/` | repo when the workspace is one; otherwise inferred from content |

Many sessions and workspaces feed one project; one generic folder can feed many projects.

---

## Design

- **One command:** `memex init` sets everything up. Bare `memex` shows status. `doctor` checks the setup. Internal plumbing is hidden from `--help`.
- **Zero maintenance:** `reflect` runs detached after every session; `tidy` consolidates near-duplicates weekly. Nothing to remember to run.
- **SCHEMA.md is the contract:** the vault ships a schema written for agents — layout, page types, frontmatter, store-vs-skip, supersession. Any agent that reads it can maintain the brain.
- **Recall that behaves:** IDF-weighted ranking with bilingual stemming, per-session dedup, absolute file paths for immediate `Read`.
- **Windows-first portability:** `DETACHED_PROCESS`, UTF-8 forced on stdio, `OpenProcess` for pid-liveness, quote-safe hook commands.
- **CLI:** zero runtime dependencies (stdlib only). Install via `uv tool install` / `pipx` / `pip`.
- **Vaults:** local & private. `raw/` + `wiki/` + `now/` live here, not in your code repos.
- **Providers:** `claude` CLI or any OpenAI-compatible endpoint. LLM runs only in `reflect`/`synth`; every hook on the session's critical path is LLM-free and exits 0 on error.
- **Routes by content type:** sessions are **distilled**, documents & media are **adopted** (pdf/docx/pptx/images/audio via markitdown/whisper/tesseract, local only), code is **analyzed**, config is **skipped**.
- **Trust tiers:** `bronze` (raw) / `silver` (sessions/docs) / `gold` (code/curated, edited with audit).

---

## How Claude uses it (the skill)

`memex init` installs a user skill (`~/.claude/skills/memex/`). Claude reaches for it when memory matters:

| You say… | Claude does |
|---|---|
| *"how did we decide X?"* | `memex search "…"` → Reads returned pages |
| *"who owns X?"* | searches entities/decisions, follows `[[wikilinks]]` |
| *"remember this"* | `memex remember "…"` → wiki page immediately |
| *"save where we left off"* | writes structured handoff → `memex handoff --stdin` |
| *"save today's agenda"* | pipes it → `memex briefing --stdin` |

`/memex` also works as a slash command. Everything the skill does, you can do from any terminal.

---

## Make it yours

The vault root has an `ABOUT.md` — your role, day-to-day, what you care about, language, teams. The synthesizer injects it into every distill/propose call, so knowledge is judged from your perspective. Edit the file, change the brain.

---

## Obsidian

The vault **is** an Obsidian vault — point Obsidian at it and you get graph view, backlinks, and search. Exclude `raw/` from Obsidian's settings (it's the forensic layer, noisy by design).

Already have an Obsidian vault? `memex init --vault <your-vault>` is non-destructive — it only adds its files. To fold existing notes in: `memex ingest --vault <your-vault> --docs <folder>`.

---

## Testing

```bash
python -m unittest discover -s tests        # no LLM, no network — mock provider
bash tests/live_e2e.sh                      # live loop on the real machine (mock LLM)
```

46 tests, 0 failures.

---

## License

[MIT](LICENSE)
