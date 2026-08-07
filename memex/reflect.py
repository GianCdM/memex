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
  3. tidy  — every `tidy_every_days`, surface near-duplicate pages as an
     audit note (.memex/audit/merge-suggestions.md). Detection only —
     merging happens via review ChangeSets a human promotes.
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
    workspace, root, display_name = workspace_mod.workspace_key_detail(cwd) if cwd else (None, None, None)
    if workspace:
        _refresh_workspace(vault, workspace, root=root, display_name=display_name,
                           provider=getattr(args, "provider", None))

    # 3) hygiene: automatic consolidation on a cadence — nothing manual to remember
    if rc == 0:
        _auto_tidy(vault, lim, provider=getattr(args, "provider", None))

    # 4) embeddings: refresh vectors for new/changed pages (incremental, cheap)
    _auto_embed(vault)

    return rc or 0


def _auto_tidy(vault, lim, provider=None) -> None:
    """Surface near-duplicate candidates when it's due. Gated by cadence
    (`tidy_every_days`, 0 disables), brain size (`tidy_min_pages`), and a state
    timestamp so a burst of session-ends doesn't re-scan repeatedly.

    Automatic tidy is DETECTION ONLY: it writes the audit suggestions note
    (.memex/audit/merge-suggestions.md) and never merges or deletes anything.
    `memex tidy` (gardening.consolidate) files the same clusters as review
    ChangeSets a human can promote."""
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
    # the claim is RELEASED unless the scan actually completes, so a busy vault
    # or a config error just retries on the next reflect.
    hookio.save_state(vault, "last-tidy", {})
    print(f"auto-tidy: scanning for near-duplicates (every {every_days}d)...")
    rc = None
    try:
        n_sug = gardening.write_suggestions(vault)
        rc = 0
        if n_sug:
            print(f"  {n_sug} duplicate candidate(s) -> .memex/audit/{gardening.SUGGESTIONS_FILE}")
    except Exception as e:
        print(f"auto-tidy failed: {e}")
        rc = 1
    if rc == 0:
        hookio.save_state(vault, "last-tidy", {})  # authoritative fresh stamp
    else:
        hookio.clear_state(vault, "last-tidy")
        print("auto-tidy did not complete — it will retry on the next reflect.")


def _refresh_workspace(vault, workspace, *, root=None, display_name=None, provider=None) -> None:
    raw_path, meta = workspace_mod._raw_candidate(vault, workspace)
    if not raw_path:
        print(f"workspace/{workspace}: no session capture found — nothing to refresh.")
        return
    try:
        page, incremental, delta_chars = workspace_mod.refresh_incremental(
            vault, workspace, raw_path, session_id=meta.get("id"), root=root,
            display_name=display_name, provider=provider)
    except providers.ProviderError as e:
        print(f"workspace/{workspace}: provider error, keeping previous page: {e}")
        return
    mode = "incremental" if incremental else "rebuild"
    print(f"workspace/{workspace}: refreshed -> {page} ({mode}, {delta_chars} new chars)")


def _latest_session_raw(vault, workspace):
    """Body of the newest raw session note belonging to this workspace."""
    return workspace_mod.latest_session_raw(vault, workspace)


def _refresh_now(vault, workspace, *, root=None, display_name=None, provider=None):
    """Backward-compatible alias for integrations that used the old helper."""
    return _refresh_workspace(vault, workspace, root=root, display_name=display_name, provider=provider)


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
