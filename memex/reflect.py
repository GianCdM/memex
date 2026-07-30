"""memex reflect — the detached post-session worker (the only LLM stage).

Spawned fire-and-forget by `memex capture` after a session ends. Does ALL the
slow thinking OUTSIDE the harness, so nothing ever blocks a session and there
is no maintenance for the user to remember:

  1. synth — compile pending raw notes into the wiki (long-term memory).
     Processes the whole BACKLOG (bounded per run by `reflect_max_notes`), not
     just today's notes — an offline week or a failed provider never leaves
     notes stranded waiting for a manual `memex synth`.
  2. workspace — refresh the project's workspace-page from the freshest session capture
     (short-term memory).
  3. tidy  — every `tidy_every_days`, consolidate near-duplicate pages
     (recoverable: absorbed pages archive to .memex/history/gardening/).
  4. embed — incremental: re-embed new/changed pages so semantic recall
     stays current (silent when not configured).
  5. log   — human-readable lines in the vault's log.md.

Also runnable by hand (`memex reflect --vault V --cwd .`) and safe to run
concurrently — synth AND tidy serialize on the same per-vault lock; a reflect
that finds the vault busy skips that stage and retries on the next run.
"""

from __future__ import annotations

import time
from argparse import Namespace
from pathlib import Path

from . import gardening
from . import hookio
from . import limits as limits_mod
from . import workspace as workspace_mod
from . import providers
from . import synth as synth_mod


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1
    lim = limits_mod.load(vault)

    # 1) long-term: the whole pending backlog, cost-bounded per run
    rc = synth_mod.run(Namespace(
        vault=str(vault), provider=getattr(args, "provider", None),
        limit=getattr(args, "limit", None) or lim["reflect_max_notes"],
        since=getattr(args, "since", None),
        model_propose=None, model_merge=None,
        workers=getattr(args, "workers", None),
    ))

    # 2) short-term: refresh the workspace-page for the session's project
    cwd = getattr(args, "cwd", None)
    project = workspace_mod.project_key(cwd) if cwd else None
    if project:
        _refresh_now(vault, project, provider=getattr(args, "provider", None))

    # 3) hygiene: automatic consolidation on a cadence — nothing manual to remember
    if rc == 0:
        _auto_tidy(vault, lim, provider=getattr(args, "provider", None))

    # 4) embeddings: refresh vectors for new/changed pages (incremental, cheap)
    _auto_embed(vault)

    return rc or 0


def _auto_tidy(vault, lim, provider=None) -> None:
    """Run consolidation when it's due. Gated by cadence (`tidy_every_days`,
    0 disables), brain size (`tidy_min_pages`), and a state timestamp so a
    burst of session-ends doesn't tidy repeatedly."""
    every_days = lim["tidy_every_days"]
    if not every_days:
        return
    try:
        import json
        pages = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8")).get("pages", [])
    except Exception:
        return
    if len(pages) < lim["tidy_min_pages"]:
        return
    state = hookio.load_state(vault, "last-tidy")
    last = state.get("_ts") or 0
    if time.time() - last < every_days * 86400:
        return
    # claim the slot BEFORE running so concurrent reflects don't double-tidy;
    # the claim is RELEASED unless tidy actually completes, so a busy vault
    # (rc=3), a down provider (rc=2) or a config error (rc=1) just retries on
    # the next reflect instead of silently burning the cadence.
    hookio.save_state(vault, "last-tidy", {})
    print(f"auto-tidy: consolidating near-duplicates (every {every_days}d)...")
    rc = None
    try:
        rc = gardening.consolidate(vault, provider=provider)
    except Exception as e:
        print(f"auto-tidy failed: {e}")
    if rc == 0:
        hookio.save_state(vault, "last-tidy", {})  # authoritative fresh stamp
    else:
        hookio.clear_state(vault, "last-tidy")
        print("auto-tidy did not complete — it will retry on the next reflect.")


def _refresh_now(vault, project, provider=None) -> None:
    lim = limits_mod.load(vault)
    raw = _latest_session_raw(vault, project)
    if not raw:
        print(f"workspace/{project}: no session capture found — nothing to refresh.")
        return
    try:
        body = workspace_mod.generate(vault, project, raw, provider=provider)
    except providers.ProviderError as e:
        print(f"workspace/{project}: provider error, keeping previous page: {e}")
        return
    p = workspace_mod.write_workspace(vault, project, body, author="auto")
    print(f"workspace/{project}: refreshed -> {p}")


def _latest_session_raw(vault, project):
    """Body of the newest raw session note belonging to this workspace."""
    return workspace_mod.latest_session_raw(vault, project)


def _auto_embed(vault) -> None:
    """Re-embed new and changed pages so semantic recall stays current.
    Incremental (content-hash gated) — a typical synth run re-embeds ~3-5
    pages in ~2s. Silent when embeddings are not configured."""
    from . import embed as embed_mod
    from argparse import Namespace
    try:
        embed_mod.run(Namespace(vault=str(vault), force=False, dry_run=False))
    except Exception:
        pass  # never let a broken embed break a session end
