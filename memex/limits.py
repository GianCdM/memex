"""memex/limits.py — every behavioral limit in ONE place.

These used to be magic numbers scattered through the code. Now they're named,
grouped, and documented here. Two ways to change one:

  1. edit a value below (the install is editable → it takes effect immediately), or
  2. override per-vault WITHOUT touching code — add a "limits" block to the
     vault's .memex/config.json, e.g.
         { "limits": { "raw_excerpt_chars": 12000, "analyze_max_module_pages": 200 } }
     anything you don't set falls back to the default here.

Cosmetic constants (hash lengths, slug length, date slicing) are intentionally
NOT here — they don't change behavior. These are the knobs that actually do.
"""

from __future__ import annotations

DEFAULTS = {
    # ── synth · raw -> wiki ────────────────────────────────────────────────
    "raw_excerpt_chars": 50000,    # how much of EACH raw note the MERGE sees (distill/adopt)
    "raw_propose_chars": 12000,    # how much the PROPOSE classifier sees — routing is a coarse
                                   # decision (slug/section/tags), so a small budget is enough and
                                   # cuts long-session input ~4x; the merge keeps the full budget
                                   # where content fidelity actually lives.
    # NOTE: no delta_min_chars knob. An append-only re-capture whose NEW tail is
    # EMPTY is superseded without LLM (deterministic); a short-but-material tail
    # (a decision, a correction) is always delta-merged — the verifier's
    # `value: same` contract catches true no-ops, never a length threshold.
    "verify_workers": 2,           # cap on CONCURRENT strong-judge (verify_model) calls per synth
                                   # run. The cheap flash judge is not capped (it shares the worker
                                   # pool); only the expensive final judge is, so a 4-worker run
                                   # doesn't fire 4 strong calls at once. 0 = uncapped.
    "skip_pipeline_artifacts": True,  # drop raw captures of the memex's OWN synthesis workers
                                      # (a `claude -p` propose/merge/workspace whose SessionEnd got
                                      # snapshotted) BEFORE any LLM call. They are pipeline feedback,
                                      # not durable knowledge — the source session/doc exists as its
                                      # own raw. Set False to re-enable them (not recommended).
    "provider_error_cap": 3,        # a raw that fails provider N consecutive times (across
                                    # runs) is PARKED (marked done + park ChangeSet) so it
                                    # never reprocesses infinitely. 0 = never park (legacy).
    "verify_strong_body_chars": 8000,  # a proposed body larger than this always goes to the
                                       # strong judge (more room for invention).
    "verify_source_chars": 12000,      # how much of the SOURCE doc the fidelity verifier sees
                                       # (a delta merge overrides this with the appended tail).
    "chunk_chars": 50000,             # a session (or append tail) larger than this is processed in
                                       # sequential 50k chunks — each chunk proposes/merges/verifies
                                       # independently, so a giant session's middle is never truncated.
    "max_tags": 8,                # tags kept per page
    "slug_max": 60,               # max slug length
    "synth_workers": 4,           # parallel LLM workers per synth run (1 = sequential)
    # Index neighbors shown to the propose model. 0 = full index (legacy, large
    # prompts); N>0 = only the top-N pages most related to the raw note via the
    # lexical scorer. A small number (e.g. 20) keeps dedupe/wikilink context
    # without the whole catalog inflating every prompt.
    "index_neighbors": 20,

    # ── analyze · code -> architecture ─────────────────────────────────────
    # analyze writes 1 overview + one page per SIGNIFICANT module. It SCALES with
    # the repo (a big monorepo -> dozens/hundreds of module pages) but NEVER a
    # page per file — each page is a module-level synthesis.
    "analyze_max_module_pages": 80,   # ceiling on module pages (raise for huge monorepos)
    "analyze_module_min_files": 3,    # a dir needs >= this many code files to earn a page
    "analyze_module_depth": 2,        # descend into monorepo containers (src/packages/…) up to here
    "analyze_files_per_module": 80,   # file names listed inside a module digest
    "analyze_overview_chars": 12000,  # overview digest budget
    "analyze_module_chars": 8000,     # per-module digest budget
    "analyze_keyfile_chars": 2500,    # chars read from each manifest / README
    "analyze_tree_dirs": 50,          # dirs shown in the top-level tree

    # ── retrieve · recall hook ─────────────────────────────────────────────
    "retrieve_max_results": 5,        # pages injected per prompt
    "retrieve_min_score": 0.05,       # relevance gate (Jaccard)
    "retrieve_min_overlap": 2,        # shared terms required
    "retrieve_min_prompt_chars": 15,  # skip terse prompts ("ok", "vai")

    # ── boot · SessionStart working-memory injection ───────────────────────
    "boot_workspace_max_age_days": 14,  # a workspace-page older than this stays out of boot
    "boot_max_chars": 8000,           # cap on injected workspace-page body
    "boot_raw_tail_chars": 0,         # optional raw fallback; 0 keeps boot workspace-only

    # ── workspace · working memory (workspace-page) ─────────────────────────
    "workspace_source_chars": 500000,   # transcript tail the generator model sees (effectively unlimited)
    "workspace_max_chars": 8000,        # cap on a generated workspace-page body (~120 lines)

    # ── reflect · the detached post-session worker ─────────────────────────
    "reflect_max_notes": 300,         # backlog notes per reflect run. 30 was a cost bound from the
                                      # pro-merge / human-approval era; with flash propose+merge and
                                      # auto_review ON the limit only paces the run, so a bigger
                                      # default drains the backlog in few runs instead of ~52.

    # ── tidy · automatic consolidation of near-duplicate pages ─────────────
    "tidy_every_days": 7,             # auto-consolidation cadence (0 = never auto-tidy)
    "tidy_min_pages": 12,             # don't bother tidying a brain smaller than this
    "garden_suggest_threshold": 0.3,  # near-dup DETECTION (writes .memex/audit/merge-suggestions.md)
    "garden_merge_threshold": 0.4,    # near-dup MERGE (auto-tidy and `memex tidy`)
    "garden_merge_chars": 20000,       # how much of EACH clustered page the merge model sees
    "garden_semantic_threshold": 0.85, # cosine cutoff for cross-language duplicate detection
                                       # (requires precomputed embeddings; ignored otherwise)

    # ── providers ──────────────────────────────────────────────────────────
    "llm_timeout_seconds": 600,
}


def load(vault=None) -> dict:
    """DEFAULTS merged with the vault's optional config.json 'limits' block.
    Unknown keys in the override are ignored (typo-safe)."""
    out = dict(DEFAULTS)
    if vault is not None:
        try:
            from . import config as config_mod
            override = (config_mod.load_vault(vault) or {}).get("limits") or {}
            out.update({k: v for k, v in override.items() if k in DEFAULTS})
        except Exception:
            pass
    return out
