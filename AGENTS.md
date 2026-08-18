# AGENTS.md — memex

Operational briefing for AI agents working on this repo. Complements `README.md`
(which is for humans). If you are a Claude Code session, `CLAUDE.md` also applies.

## What this project is

`memex` is a local-first, stdlib-only "second brain" CLI. It captures AI sessions
via Claude Code hooks and synthesizes them into a searchable Markdown wiki
(`wiki/`) plus a per-workspace working-memory page (`workspace/`) that a fresh
session reads first. Completions go exclusively through the `claude` CLI;
embeddings (semantic recall) go through a separate HTTP `/embeddings` endpoint.

## The golden rules

- **Stdlib only.** `pyproject.toml` `dependencies = []` — never add a runtime
  dependency. Everything must run on Python 3.10+ with the standard library.
- **Hooks never block a session.** Every hook on the critical path
  (boot / recall / capture) is LLM-free and must exit 0 on error.
- **Raw evidence is immutable.** `.memex/raw/` is the forensic layer — never
  edit or regenerate an existing raw capture.
- **Config is flat and shared.** `~/.config/memex/config.json` (global) and
  `.memex/config.json` (vault) share the same shape: top-level `models.*` +
  `embeddings.*`. Vault overrides global per key; `null` in the vault suppresses
  the inherited value (notably in `embeddings`).
- **Language:** source code, comments, and commit messages in English. Wiki/
  raw content keeps whatever language the source session used (often PT).

## How to run the tests

```bash
python3 -m unittest discover -s tests    # 187 tests, no LLM, no network
```

`memex.providers.complete` is patched in the test suite (no real `claude -p`
calls); embeddings operate on precomputed fixtures.

## Architecture — where things live

| Module | Role |
|---|---|
| `capture.py` | hook payload → `.memex/raw/` (immutable, scrubbed, LLM-free) |
| `reflect.py` | detached post-session worker: synth + workspace-page + tidy + embed |
| `synth.py` | raw → wiki (propose → merge → verify, each via `claude -p`) |
| `providers.py` | the ONLY LLM/embeddings I/O; `complete()` always shells `claude -p` |
| `config.py` | flat config resolution + overlay of global/vault; `resolve_models` / `resolve_embeddings` |
| `recall.py` / `search.py` | lexical (IDF) + optional semantic (cosine / RRF) search |
| `embed.py` | re-vectorizes changed pages; incremental via content-hash |
| `ingest.py` / `extract.py` | adopt docs & media (pdf/docx/pptx/audio/OCR) |
| `mcp_server.py` | exposes `search`, `remember`, `status`, etc. to Claude |

Lifecycle: `capture` (hook) → `.memex/raw/` → `reflect` (LLM) → `wiki/` +
`workspace/`. Every hook on the user-facing path stays out of it.

## CLI surface

```
analyze  audit  boot  capture  config  doctor  embed  fresh-start  health
hook  ingest  log  mcp  metrics  new  recall  reflect  relink  retrieve
review  search  skill  status  synth  vault
```

`memex doctor` checks setup; `memex status` shows what's in the brain.

## Versioning & release (semantic-release) — READ BEFORE COMMITTING

Every push to `main` triggers the release workflow (`.github/workflows/release.yml`),
which reads the **conventional commits** since the last tag and bumps the version.
The version's single source of truth is `pyproject.toml [project] version`
(`memex/__init__.py` reads it via `importlib.metadata`, with a pyproject fallback
for source runs). The tag is the release point; there is **no GitHub Release
page** (tag + `CHANGELOG.md` only — `upload_to_vcs_release` is off).

Write every commit message with the conventional prefix so the bump is deliberate:

| Prefix | Bump | Example |
|---|---|---|
| `feat:` | MINOR | `feat: add semantic recall to search` |
| `fix:` | PATCH | `fix: migrate old provider shape first` |
| `feat`/`fix` + `BREAKING CHANGE:` | MAJOR | `feat: rework API (BREAKING CHANGE: ...)` |
| `chore:` `docs:` `refactor:` `test:` `ci:` | none | `refactor: simplify complete()` |

Rules:
- **Scope optional** but encouraged on touchy areas: `fix(cli):`, `feat(config):`.
- **`refactor:`, `chore:`, `docs:`, `test:`, `ci:` do NOT bump** — safe for
  non-releasing work.
- A breaking change MUST be signaled with `BREAKING CHANGE:` in the body/footer
  (not just a `!`), so the parser catches it and bumps MAJOR.
- The workflow commits the bump with `[skip ci]` and tags `vX.Y.Z`; it will not
  re-trigger itself. There is no manual version edit — let the workflow own the bump.
- Don't create or move tags manually for releases (except the initial baseline),
  and never edit `pyproject.toml` version by hand.
- One push = at most one release. If unsure whether a change is `feat`, `fix`,
  or none, prefer `refactor:`/`chore:` (no bump) unless the user wants a release.

## Config gotchas

- `embeddings.timeout` (default `60`) is passed to the `/embeddings` HTTP call; a
  slow gateway (e.g. corporate GenPlat) may need `120`+. The same `settings`
  dict feeds `claude -p` (default `600`).
- `embeddings.input_type` / `query_input_type` are sent verbatim — **no magic
  mapping**. OpenRouter/Nvidia: `passage`/`query`; GenPlat/Cohere:
  `search_document`/`search_query`; OpenAI/Voyage: omit both.
- The compat shim (`config._migrate_cfg`) reads old `provider.*` / nested
  `{model, dense}` shapes in memory for one release — don't re-add them.