"""memex boot — the SessionStart hook: wake the brain up.

Whatever this prints on stdout is injected into the new session's context by
the harness. It's the fix for the resume problem: a prompt like "bora continuar"
carries no topical words for recall to match, but by then the session ALREADY
knows where you left off, because boot injected the project's workspace-page
(working memory) plus how to reach the rest of the brain.

Fires on source=startup|resume|clear. On source=compact the conversation
continues with its own summary, so boot stays silent. Silent, exit-0 behavior
on ANY problem — session start must never be blocked or noisy.
"""

from __future__ import annotations

from pathlib import Path

from . import config as config_mod
from . import hookio
from . import limits as limits_mod
from . import workspace as workspace_mod


def run(args) -> int:
    try:
        return _run(args)
    except Exception:
        return 0  # never break a session start


def _run(args) -> int:
    payload = hookio.read_payload()
    source = (payload.get("source") or "").lower()
    if source == "compact":
        return 0  # conversation continues; its summary already carries state

    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        return 0
    lim = limits_mod.load(vault)
    hookio.prune_state(vault)  # housekeeping: drop stale per-session files

    cwd = payload.get("cwd") or str(Path.cwd())
    workspace, root, display_name = workspace_mod.workspace_key_detail(cwd)
    workspace = workspace or "workspace"
    display_name = display_name or workspace

    parts = []

    # 1) working memory — where we left off in THIS workspace
    meta, body = workspace_mod.read_workspace(vault, workspace, cwd=cwd)
    now_fresh = bool(body and _fresh(meta, lim["boot_workspace_max_age_days"]))
    if now_fresh:
        body = body.strip()[: lim["boot_max_chars"]]
        parts.append(
            f"## Where we left off — workspace `{display_name}` (`{workspace}`) "
            f"(saved {meta.get('updated', '?')}, by {meta.get('author', '?')})\n"
            f"{body}\n"
            f"(full page: {workspace_mod.workspace_path(vault, workspace)})"
        )

    # 2) raw safety net — opt-in and only when the distilled now-page is absent,
    # stale, or behind the latest captured session. Never inject the archive by
    # default: raw is forensic and may contain tool noise/dead ends.
    raw_tail_chars = lim.get("boot_raw_tail_chars", 0)
    raw_tail = None
    if raw_tail_chars:
        candidate, raw_meta = workspace_mod._raw_candidate(vault, workspace)
        needs_fallback = not now_fresh
        if now_fresh and meta.get("author") != "handoff":
            needs_fallback = workspace_mod.raw_is_newer_than_workspace(raw_meta, meta)
        if candidate and needs_fallback:
            raw_tail = workspace_mod.latest_session_raw_tail(
                vault, workspace,
                max_chars=raw_tail_chars,
                max_age_days=lim["boot_workspace_max_age_days"],
            )
    if raw_tail:
        parts.append(
            f"## Recent raw capture — workspace `{workspace}`\n"
            "The following is an unsynthesized safety-net excerpt; treat it as "
            "provisional context and read the full file only if needed.\n"
            f"{raw_tail['body']}\n"
            f"(full raw: {raw_tail['path']})"
        )

    # 3) long-term memory pointers use the semantic project label, not the
    # technical workspace key. Content-inferred projects still surface via recall.
    project = workspace_mod.project_key(cwd)
    hub = vault / ".memex" / "views" / "projects" / f"{project}.md" if project else None
    n_pages = _count_pages(vault, project) if project else 0
    if hub and hub.is_file():
        parts.append(f"Project hub ({n_pages} wiki page(s) on `{project}`): {hub}")
    elif n_pages:
        parts.append(f"{n_pages} wiki page(s) mention `{project}` — `memex search` finds them.")

    if not parts:
        return 0  # empty brain for this project — stay out of the way

    out = [
        "<memex-brain>",
        f"memex — your second brain for this machine lives at: {vault}",
        "",
        *parts,
        "",
        "Use it deliberately:",
        '- find knowledge:  memex search "<terms>"   (then Read the page paths)',
        f"- conventions:     {vault / 'SCHEMA.md'}",
        "</memex-brain>",
    ]
    print("\n".join(out))
    return 0


def _age_hours(meta) -> float:
    from datetime import datetime
    try:
        updated = datetime.strptime((meta or {}).get("updated", ""), "%Y-%m-%dT%H:%M:%SZ")
        return (datetime.utcnow() - updated).total_seconds() / 3600.0
    except Exception:
        return 0.0  # unknown age — better to show than to hide


def _fresh(meta, max_age_days) -> bool:
    return _age_hours(meta) <= int(max_age_days) * 24


def _count_pages(vault, project) -> int:
    import json
    try:
        idx = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8"))
        return sum(1 for p in idx.get("pages", []) if p.get("project") == project)
    except Exception:
        return 0
