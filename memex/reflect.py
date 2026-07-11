"""memex reflect — the detached post-session worker (the only LLM stage).

Spawned fire-and-forget by `memex capture` after a session ends. Does the slow
thinking OUTSIDE the harness so nothing ever blocks a session:

  1. synth  — compile today's new raw notes into the wiki (long-term memory);
  2. now    — refresh the project's now-page from the freshest session capture
              (short-term memory), unless a deliberate handoff is fresh;
  3. log    — one line in the vault's log.md.

Also runnable by hand (`memex reflect --vault V --cwd .`) and safe to run
concurrently — synth holds a per-vault lock, and a second reflect just no-ops.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import date
from pathlib import Path

from . import limits as limits_mod
from . import now as now_mod
from . import providers
from . import synth as synth_mod
from . import vault as vault_mod


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1
    since = getattr(args, "since", None) or date.today().isoformat()

    # 1) long-term: raw -> wiki (bounded to the fresh notes; lock-protected)
    rc = synth_mod.run(Namespace(
        vault=str(vault), provider=getattr(args, "provider", None),
        limit=getattr(args, "limit", None), since=since,
        model_propose=None, model_merge=None,
    ))

    # 2) short-term: refresh the now-page for the session's project
    cwd = getattr(args, "cwd", None)
    project = now_mod.project_key(cwd) if cwd else None
    if project:
        _refresh_now(vault, project, provider=getattr(args, "provider", None))

    return rc or 0


def _refresh_now(vault, project, provider=None) -> None:
    lim = limits_mod.load(vault)
    if now_mod.hold_active(vault, project, lim["now_handoff_hold_hours"]):
        print(f"now/{project}: a fresh deliberate handoff exists — keeping it.")
        return
    raw = _latest_session_raw(vault, project)
    if not raw:
        print(f"now/{project}: no session capture found — nothing to refresh.")
        return
    try:
        body = now_mod.generate(vault, project, raw, provider=provider)
    except providers.ProviderError as e:
        print(f"now/{project}: provider error, keeping previous page: {e}")
        return
    p = now_mod.write_now(vault, project, body, author="auto")
    print(f"now/{project}: refreshed -> {p}")


def _latest_session_raw(vault, project):
    """Body of the newest raw SESSION note belonging to this project (raw
    filenames start with the date, so name-sort ≈ time-sort; mtime breaks ties)."""
    best, best_key = None, None
    for f in (vault / "raw").glob("*.md"):
        name = f.name
        if "--doc--" in name or "--code--" in name:
            continue
        try:
            meta, body = synth_mod._read_frontmatter(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if meta.get("source") not in ("claude", "cursor", "codex"):
            continue
        if now_mod.project_key(meta.get("cwd")) != project:
            continue
        try:
            key = (name[:10], f.stat().st_mtime)
        except OSError:
            key = (name[:10], 0)
        if best_key is None or key > best_key:
            best, best_key = body, key
    return best
