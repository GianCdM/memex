# memex

> Your second brain for AI sessions — decisions, architecture, people, projects, and code, turned into a searchable Markdown wiki and working memory that picks up where you left off.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What it is

`memex` wires into Claude Code sessions and turns them into durable, navigable knowledge — without leaving your machine.

- **Long-term memory:** a Markdown wiki (`wiki/`) with decisions, entities, projects, and facts. Opens in Obsidian, searchable from the terminal and by the AI.
- **Working memory:** a per-workspace workspace-page (`workspace/<workspace>.md`) injected at session start — "where we left off," no re-explaining.
- **Episodic memory:** immutable, LLM-free raw transcripts (`.memex/raw/`) kept as forensic source material.

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

The `claude` provider needs the Claude Code CLI logged in once (`claude` → `/login`).

Upgrade: `uv tool upgrade memex`.

---

## Quickstart

```bash
memex doctor          # check setup
memex init            # activate this workspace (vault + hooks + skill + backfill)
                      # repeat in each workspace you work in

memex search "dedup pipeline"           # talk to the brain
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
  classDef workspacec fill:#f59e0b,color:#000,stroke:#b45309,stroke-width:2px
  classDef memexc fill:#f0abfc,color:#000,stroke:#a21caf,stroke-width:2px

  YOU["YOU — run once<br/><b>memex init</b><br/>hooks + skill + first capture"]:::you

  subgraph AUTO["then memex runs itself"]
    direction TB
    HARNESS["Claude Code session"]
    BOOT(["hook · SessionStart → <b>boot</b>"]):::hook
    RECALL(["hook · UserPromptSubmit → <b>recall</b><br/>lexical + semantic (embeddings)"]):::hook
    CAP(["hooks · SessionEnd + PreCompact → <b>capture</b>"]):::hook
    RAW[".memex/raw/ — episodic memory<br/>immutable, scrubbed, LLM-free"]:::bronze
    REFL["<b>reflect</b> — detached, the only LLM stage<br/>synth · workspace-page · tidy · embed"]:::llm
    WIKI["wiki/ — long-term memory<br/>topics · entities · decisions"]:::wiki
    VIEWS[".memex/ — regenerated, not knowledge<br/>views/ catalogs · audit/ reports"]:::memexc
    WORKSPACE["workspace/&lt;workspace&gt;.md — working memory<br/>'where we left off'"]:::workspacec

    BOOT -- "inject workspace-page" --> HARNESS
    RECALL -- "inject wiki pages (deduped)" --> HARNESS
    HARNESS --> CAP --> RAW --> REFL
    REFL --> WIKI
    REFL --> VIEWS
    REFL --> WORKSPACE
    WORKSPACE -.-> BOOT
    WIKI -.-> RECALL
  end

  YOU --> HARNESS
```

Four hooks close the loop. `boot` injects working memory at session start. `recall` injects relevant wiki pages per prompt. `capture` saves every session (even pre-compaction). `reflect` runs detached in the background — synth (raw→wiki) + workspace-page + tidy + embed. Every hook on the session's critical path is LLM-free and exits 0 on error.

An MCP server exposes `search`, `remember` and `status` as structured tools — no Bash escaping, no stdout parsing. A user-level skill teaches Claude to reach for them deliberately.

---

## Memory layers

| Layer | Where | Lifetime | Written by |
|---|---|---|---|
| **Episodic** | `.memex/raw/` | forever, immutable | `capture` (LLM-free) |
| **Working** | `workspace/<workspace>.md` | current effort, overwritten | `reflect` (auto) |
| **Semantic** | `wiki/` | forever, curated | `reflect` / `synth` |
| **Generated views** | `.memex/views/` | regenerated, machine-owned | `reflect` / `synth` |
| **Audit** | `.memex/audit/` | regenerated, machine-owned | `gardening` |

---

## Session → workspace → project

| Claude concept | memex layer | Keyed by |
|---|---|---|
| one **session** | `.memex/raw/` | session id |
| the **workspace** it ran in | `workspace/` | collision-safe path key: Git root or current folder, relative to `HOME` |
| the **project/initiative** | `wiki/` | repo when the workspace is one; otherwise inferred from content |

Many sessions and workspaces feed one project; one generic folder can feed many projects.

---

## Design

- **One command:** `memex init` sets everything up. Bare `memex` shows status. `doctor` checks the setup. Internal plumbing is hidden from `--help`.
- **Zero maintenance:** `reflect` runs detached after every session — synth (raw→wiki) + workspace-page + tidy + embed. Nothing to remember to run.
- **SCHEMA.md is the contract:** the vault ships a schema written for agents — layout, page types, frontmatter, store-vs-skip, supersession. Any agent that reads it can maintain the brain.
- **Recall that behaves:** lexical (IDF-weighted, bilingual stemming) + optional semantic layer (precomputed embeddings with cosine RRF-fusion). Embeddings are auto-refreshed by `reflect` after every synth run. Falls back to lexical-only when not configured.
- **Windows-first portability:** `DETACHED_PROCESS`, UTF-8 forced on stdio, `OpenProcess` for pid-liveness, quote-safe hook commands.
- **CLI:** zero runtime dependencies (stdlib only). Install via `uv tool install` / `pipx` / `pip`.
- **Vaults:** local & private. `.memex/raw/` + `wiki/` + `workspace/` live here, not in your code repos.
- **Providers:** `claude` CLI or any OpenAI-compatible endpoint. LLM runs only in `reflect`/`synth`; every hook on the session's critical path is LLM-free and exits 0 on error.
- **Routes by content type:** sessions are **distilled**, documents & media are **adopted** (pdf/docx/pptx/images/audio via markitdown/whisper/tesseract, local only), code is **analyzed**, config is **skipped**.
- **Page metadata:** `kind` (session/doc/manual/code/merged — where it came from) + `status` (current/superseded/obsolete/deprecated/archived/draft — whether it still holds). Auto-maintained `## 📋 Histórico` changelog on every page.
- **Wiki integrity:** Run `memex audit --dry-run` before applying a recovery lot. Generated views and audit output are under `.memex/`, not canonical wiki pages. Existing-vault migration steps (snapshot → dry-run → human approval → apply → rollback) are in `docs/superpowers/plans/2026-08-06-wiki-integrity-migration-runbook.md`.

---

## Models & Providers

Memex uses three model roles per synthesis run — a **proposer** to decide where knowledge lives, a **merger** to write wiki prose, and a **verifier** to catch invention. They're configured per provider in `~/.config/memex/config.json`.

### How models are used

| Stage | Model | What it does |
|---|---|---|
| **Propose** (routing) | `model_propose` | Reads the raw note + wiki index, decides: which slug / section / tags, or skip. One call per note. |
| **Merge** (writing) | `model_merge` | Reads the raw note + existing page body, writes or updates the wiki page. One call per note. |
| **Verify** (fidelity) | `model_verify` | Judges body fidelity against source text. **Skipped mechanically** when the proposed body is empty, unchanged from current, or a verbatim subset of the source (0 LLM). |
| **Gardening** (tidy) | `model_merge` | Consolidates near-duplicate pages into one coherent page. |
| **Embeddings** (optional) | separate provider | Semantic recall — vector search over wiki pages. Incrementally refreshed by `reflect` after each synth run. Falls back to lexical (IDF + stemming) when disabled. |

> **Quote-optional claims.** Proposals may attach verbatim quotes to claims as evidence, but missing a quote is **unanchored**, not unsupported — unanchored claims still pass body-fidelity verification. This makes the pipeline robust across languages and paraphrased sources without needing a stronger proposer just for quote generation.

### Supported providers

**Generation (LLM):**

| Provider | Backend | How it works |
|---|---|---|
| `claude` | Claude Code CLI (`claude -p --model`) | Works out of the box. Supports MCP tools in prompts for doc resolution. |

**Embeddings (semantic recall):**

A separate HTTP provider — Anthropic's API doesn't do embeddings, so this is independent. Any OpenAI-compatible `/embeddings` endpoint works:

- **NVIDIA Nemotron Embed 1B (free, multilingual):** `base_url=https://openrouter.ai/api/v1`, `model=nvidia/nemotron-3-embed-1b`

When embeddings are disabled (`base_url` is empty or unset), recall falls back to a bilingual lexical scorer (IDF-weighted Jaccard) — zero config, zero cost, works offline.

### Configuration layers

Three layers, each overriding the previous:

```
factory defaults (config.py)
  → ~/.config/memex/config.json   (your global overrides)
    → <vault>/.memex/config.json   (per-vault overrides)
```

**Factory defaults** (`memex/config.py`):

```json
{
  "provider": {
    "order": ["claude"],
    "claude": {
      "model_propose": "haiku",
      "model_merge": "sonnet"
    },
    "embeddings": {
      "base_url": null,
      "model": null
    }
  }
}
```

**Your config** (`~/.config/memex/config.json`) — example using a corporate LLM gateway (GenPlat):

```json
{
  "provider": {
    "order": ["claude"],
    "claude": {
      "model_propose": "deepseek-v4-flash-claude",
      "model_merge": "deepseek-v4-pro-claude[1m]"
    },
    "embeddings": {
      "base_url": "https://your-gateway.corp.com/api/v2",
      "model": "embed-multilingual-v3",
      "input_type": "search_document",
      "api_key_helper": "your-token-helper --format token"
    }
  }
}
```

The `embeddings` provider is a separate HTTP endpoint — any gateway that speaks the OpenAI `/embeddings` protocol works. Corporate LLM gateways (GenPlat, etc.) proxy models like Cohere's `embed-multilingual-v3` without ever hitting the public API.

`api_key` supports three modes (checked in order):
1. `api_key_env` — name of an env var to read at runtime (e.g. `"OPENAI_API_KEY"`)
2. `api_key_helper` — shell command whose stdout is the token (same pattern as Claude Code's `apiKeyHelper` — never persists short-lived tokens)
3. `api_key` — literal string (last resort; avoid persisting secrets in config files)

**Vault overrides** (`<vault>/.memex/config.json`):

```json
{"models": {"propose": null, "merge": null, "verify": null}}
```

Set to a model name to override only that vault. Leave `null` to use the global provider config.

### Auto-review, judge model, and doc filters

The verifier that gates every ChangeSet is the **judge**. With `auto_review: true`
the judge decides everything (no human approval); set `verify_model` to a strong
model for that role. The judge is only invoked when **mechanical pre-verify**
cannot decide — the pipeline checks (0 LLM) whether the proposed body is empty,
identical to the current page, or a verbatim subset of the source. Only when
none of those apply does the LLM verifier run.

```json
{
  "auto_review": true,
  "verify_model": "deepseek-v4-pro-official",
  "ingest": {
    "docs": {
      "include": ["docs/**/*.md", "README.md"],
      "exclude": ["**/*.log", "pessoal/automation/**"],
      "skip_ids": ["**/morning-routine.log"]
    }
  }
}
```

`ingest.docs` is an allowlist/denylist for the `--docs` walk and doc-index:
`include` restricts to matching files, `exclude` drops them, `skip_ids` drops
index entries by locator. Empty/missing = legacy behavior (adopt every prose
file). This is how you keep a personal automation log or a growing `.log` out of
the wiki without hardcoding anything.

### Pipeline knobs (per-vault `limits` block)

All the behavioral limits live in `memex/limits.py` and can be overridden per
vault via a `"limits"` block in the vault config (unknown keys are ignored):

| Knob | Default | What it does |
|---|---|---|
| `raw_excerpt_chars` | 50000 | how much of a raw note the MERGE step sees |
| `raw_propose_chars` | 12000 | how much the PROPOSE classifier sees (routing is coarse → small budget) |
| `verify_workers` | 2 | cap on concurrent verify-model calls per run (0 = uncapped) |
| `verify_strong_body_chars` | 8000 | proposed bodies larger than this always go to the verifier LLM |
| `index_neighbors` | 20 | pages shown to the propose step from the index |

### Session-delta pipeline: the wiki reads the raw's progression, not its snapshot

A long session is captured many times (each PreCompact / SessionEnd writes a
full accumulated snapshot with the same `id`). Instead of re-distilling the
whole snapshot every time — which truncates the middle of a >50k session — the
wiki consumes the raw **incrementally**:

```text
Claude JSONL (append-only)
  └─ capture → raw snapshot (immutable, preserved)
       ├─ workspace: delta since its cursor → working memory
       └─ wiki synth: delta since its checkpoint → merge/verify → page
```

The workspace never becomes the wiki's source: both read the same raw, but keep
**separate checkpoints** (the workspace's per-page cursor vs the wiki's
`.memex/lineage.json` per-session `chars`+`prefix_hash`).

How a re-capture routes (in `.memex/lineage.json`):

- **strict append** (the new body's prefix still hashes to the recorded
  checkpoint) → `session-delta` / `doc-delta`: propose is **skipped**, the
  slug/section come from lineage, only the new **tail** is distilled into the
  existing page, and the verifier judges it **against the tail** with a
  dedicated distilled-delta prompt (body fidelity — a delta carries no
  per-claim anchors by design, and content already on the page is out of scope).
- **empty tail** → superseded with no LLM (a short-but-material tail is never
  dropped on a length threshold; the verifier's `value: same` catches no-ops).
- **edited / shrunk / page archived or renamed** → safe full fallback: never a
  fabricated delta, never a headless page.

Routing: only `supported` auto-applies. A `partial` session-delta (durable tail
content not reflected) **parks** for review, so the checkpoint never advances
past unreflected content; `ambiguous` parks too (an uncertain judge must never
burn a session's tail). The evidence-anchored claim gate is skipped for verified
deltas (a delta is grounded to its raw tail by body fidelity). The checkpoint
advances **only** when the delta actually applies; a provider error keeps the
raw pending for the next run.

`memex deltas` is a read-only dry-run of the historical surface — it groups
raws by session, walks the append chain (prefix hashes), and reports which
sessions already have a checkpoint vs which a chunked backfill would need to
re-read:

```bash
memex deltas --vault ~/memex
```

### Pipeline telemetry: `memex metrics`

Every synthesized raw appends a JSONL line to `.memex/metrics.jsonl`
(kind, mode, outcome, route, latency, body size, models; `mode` is `full`,
`doc-delta` or `session-delta`, with `delta_chars`/`checkpoint_before`/
`checkpoint_after` on delta rows). `memex metrics` summarizes it — counts by
outcome/route/kind/**mode**, average latency, and the active judge model — so
cost/quality decisions are grounded in real numbers:

```bash
memex metrics --vault ~/memex            # everything
memex metrics --vault ~/memex --since 2026-08-01   # last week
```

The `golden/` directory holds eval fixtures (raw → expected page) to regression
test the synth before changing budgets, models, or routing.

### Provider order & fallback

`provider.order` is a list — memex tries providers in order until one succeeds. The default is `["claude"]`: use Claude Code if available.

Run `memex doctor` to see which providers are detected on your machine:

```
Detected providers:
  claude CLI : OK  ~/.../claude
  openrouter : OK  (env OPENROUTER_API_KEY set)
```

---

## How Claude uses it (MCP + skill)

`memex init` wires an MCP server and installs a user skill. Claude gets three structured tools:

| Tool | What it does |
|---|---|
| `search` | Find pages by keyword — returns scored results with file paths. Read the path for full detail. |
| `remember` | File a fact, decision, or preference into the brain. Synthesized into a wiki page immediately. |
| `status` | Peek at the brain — raw notes, wiki pages, pending synthesis, workspace-pages. |

The skill teaches Claude *when* to use them:

| You say… | Claude does |
|---|---|
| *"how did we decide X?"* | calls `search` → Reads returned pages |
| *"who owns X?"* | calls `search`, follows `[[wikilinks]]` |
| *"remember this"* | calls `remember` with the fact |

Fallback: `memex search` from the terminal. `/memex` also works as a slash command.

---

## Make it yours

The vault root has an `ABOUT.md` — your role, day-to-day, what you care about, language, teams. The synthesizer injects it into every distill/propose call, so knowledge is judged from your perspective. Edit the file, change the brain.

---

## Obsidian

The vault **is** an Obsidian vault — point Obsidian at it and you get graph view, backlinks, and search. Raw evidence lives under `.memex/raw/` (a dot-dir, so Obsidian never lists it nor tries to render the giant session captures — no per-vault settings needed).

Already have an Obsidian vault? `memex init --vault <your-vault>` is non-destructive — it only adds its files. To fold existing notes in: `memex ingest --vault <your-vault> --docs <folder>`.

---

## Testing

```bash
python -m unittest discover -s tests        # no LLM, no network — mock provider
bash tests/live_e2e.sh                      # live loop on the real machine (mock LLM)
```

64 tests, 0 failures.

---

## License

[MIT](LICENSE)
