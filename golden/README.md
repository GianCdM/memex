# Golden set — eval fixtures for the synth pipeline

A **golden set** is a versioned collection of {raw → expected-page} fixtures used
to regression-test the synth (propose + merge + verify) BEFORE changing budgets,
models, or routing — so a cost or latency optimization can't silently degrade
wiki quality.

## Layout

```
golden/
  README.md
  sessions/            # raw session notes (kind: session)
    2026-…--claude--…--….md
  docs/                # raw doc-adopt notes (kind: doc)
  expected/            # the canonical page each raw SHOULD produce
    <slug>.md
```

Every fixture is a real or representative raw note from the vault. Expected
pages are the *curated* ideal (what a careful human/wiki would keep), not
whatever the current pipeline happens to emit.

## How to run an eval

1. Scaffold a scratch vault:

   ```bash
   memex vault new /tmp/eval && cp -r golden/*.md golden/sessions /tmp/eval/raw/ 2>/dev/null || true
   ```

2. Synthesize with the CURRENT settings:

   ```bash
   memex reflect --vault /tmp/eval --limit 50   # or: memex synth --vault /tmp/eval
   ```

3. Compare:

   ```bash
   memex metrics --vault /tmp/eval               # cost/latency/outcome telemetry
   diff -r golden/expected /tmp/eval/wiki/topics  # or the section in question
   ```

4. Judge quality by hand (or with a reviewer): did each page keep the durable
   facts, decisions, and corrections? Are there invented sections? Overly long
   pages? Wrong slug/section?

## Adding a fixture

Copy a raw note into `sessions/` or `docs/` and write the ideal page into
`expected/`. Name the expected page by its canonical slug. Add a one-line note
in the fixture frontmatter if it stresses a specific case (long session,
decision-heavy, near-duplicate of an existing page, append-only doc re-capture,
…). Fixtures are cheap to run — use them whenever you touch prompts, limits
(`raw_propose_chars`, `raw_excerpt_chars`, `delta_min_chars`, …), the verifier,
or the routing logic.

## What to watch when optimizing

- **Recall of durable content**: decisions + rationale, action items,
  corrections, ownership. Long sessions must not lose mid-conversation facts
  when the excerpt budget shrinks.
- **Fidelity (docs)**: adopted docs must not gain invented sections or drop
  significant ones — the verify gate's whole job.
- **No page explosion**: a fixture must reuse the expected slug, never spawn
  `-guide`/`-v2`/`note-…` near-duplicates.
- **No regression on no-ops**: an append-only re-capture must NOT produce a new
  page update when the append has no material content.
