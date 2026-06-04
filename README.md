# memex

> A portable, local-first **second brain** built automatically from your AI coding sessions.

`memex` turns the conversations you have with AI coding tools (Claude Code, Cursor, Codex) — plus your code and notes — into a navigable **Markdown wiki** you own forever and can open in [Obsidian](https://obsidian.md). Inspired by Vannevar Bush's *memex* (1945) and Andrej Karpathy's [LLM-Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) idea.

**Status:** working end-to-end (v0.0.1). The full loop — capture → `ingest` → `synth` → `gardening` → `retrieve`, with per-workspace hooks — is built and exercised on a real ~2k-session brain. Codebase-as-architecture (`analyze`) and continuous synth (`cron`) are next.

## Why

Most AI sessions start from zero — you re-explain decisions you already made. `memex` captures them once and compounds them into knowledge that:

- **is yours & portable** — plain Markdown + (optional) git. No vendor lock-in. Survives any tool or employer.
- **feeds itself** — hooks capture sessions automatically (LLM-free); a synthesis step compiles them into the wiki.
- **comes back to you** — relevant pages are injected into new sessions automatically.

## How it works (in a sentence)

Sessions / code / docs land verbatim in `raw/` → an LLM compiles them into a curated `wiki/` → you read & curate it in Obsidian, and it gets pulled back into your next session.

```
sources → raw/ (forensic) → synth → wiki/ (knowledge) → Obsidian + auto-recall
```

## Architecture

```mermaid
flowchart TB
  classDef bronze fill:#cd7f32,color:#fff,stroke:#8b5a2b,stroke-width:2px
  classDef wiki fill:#cbd5e1,color:#000,stroke:#475569,stroke-width:3px
  classDef llm fill:#7c3aed,color:#fff,stroke:#5b21b6,stroke-width:2px
  classDef hook fill:#0ea5e9,color:#fff,stroke:#0369a1,stroke-width:2px
  classDef road fill:#fef9c3,color:#000,stroke:#ca8a04,stroke-dasharray:5 3
  classDef store fill:#f1f5f9,color:#000,stroke:#94a3b8

  HARNESS["Harness — where you work<br/>Claude Code · Cursor · Codex<br/>(opt-in per workspace)"]
  SE(["hook SessionEnd"]):::hook
  ING["memex ingest — LLM-free, idempotent<br/>parsers → scrub (regex) → dedupe (ledger)"]
  RAW["raw/ — BRONZE<br/>transcripts + docs · immutable · scrubbed"]:::bronze
  SYN["memex synth — the ONLY LLM step (cwd-isolated)<br/>propose (haiku/qwen2.5) → merge (sonnet/qwen3)"]:::llm
  PROV["Provider — pluggable, decoupled from tier<br/>claude -p (subscription) · ollama (local)"]
  WIKI["wiki/ — SILVER / GOLD (Obsidian)<br/>topics · entities · decisions<br/>frontmatter + wikilinks + index.md"]:::wiki
  GARD["gardening · near-dups → LLM-merge"]:::llm
  UPS(["hook UserPromptSubmit · retrieve (Jaccard)"]):::hook
  STATE[".memex/ — index · ledger · synthed · changelog · history"]:::store

  HARNESS -->|SessionEnd| SE --> ING --> RAW --> SYN --> WIKI
  PROV -. feeds .-> SYN
  WIKI <--> GARD
  WIKI --> UPS
  UPS -. injects relevant pages .-> HARNESS
  ING -.-> STATE
  SYN -.-> STATE

  subgraph RD["Roadmap"]
    direction LR
    AN["analyze · code → C4 architecture"]:::road
    AD["ADRs from sessions"]:::road
    RG["code-aware RAG"]:::road
    CR["cron · continuous synth"]:::road
  end
  RD -.-> WIKI
```

The **live loop** (solid arrows): `Harness → SessionEnd → ingest → raw/ → synth → wiki/ → UserPromptSubmit (retrieve) → back to Harness`. Secrets are scrubbed at ingest (deterministic regex), the provider is decoupled from the medallion tier, and synth runs cwd-isolated so it never triggers its own capture hooks.

## Design

- **CLI (this repo):** open-source, vendor- & OS-agnostic. Install via `uv tool install memex` / `pipx` / `pip`.
- **Vaults (your data):** local & private; `raw/` + `wiki/` live here, not in your code repos.
- **Providers:** pluggable — `claude` CLI or any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, cloud). The LLM only runs in the synth step; capture hooks are free.
- **Trust tiers (medallion):** `bronze` (raw) / `silver` (sessions/docs, edited freely) / `gold` (code/curated, edited with plan + audit).

## Quickstart (current)

```bash
# detect your environment and get a recommended provider/model setup
memex doctor

# create your brain
memex vault new ~/vaults/personal
```

## Roadmap

- [x] `vault new`, `doctor`, `init` (one-command onboarding)
- [x] `ingest` — sessions (Claude / Cursor / Codex) + codebase + docs (LLM-free, scrubbed, idempotent)
- [x] `synth` — raw → wiki (2-phase, provider-pluggable: `claude` + OpenAI-compatible / Ollama)
- [x] `retrieve` + capture hooks (opt-in per workspace, merge-safe)
- [x] `gardening` — consolidate near-duplicate pages (cluster + LLM-merge)
- [x] `config`, `status`, `log`
- [ ] `analyze` — synthesize a codebase into a few architecture pages (C4-style), **not** a page per file
- [ ] ADRs / design decisions synthesized from session transcripts
- [ ] code-aware RAG for on-demand, file-level detail
- [ ] nightly `cron` for continuous synth · gold `revert`

## License

[MIT](LICENSE)
