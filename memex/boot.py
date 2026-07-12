"""memex boot — the SessionStart hook: wake the brain up.

Whatever this prints on stdout is injected into the new session's context by
the harness. It's the fix for the resume problem: a prompt like "bora continuar"
carries no topical words for recall to match, but by then the session ALREADY
knows where you left off, because boot injected the project's now-page
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
from . import now as now_mod


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
    workspace = now_mod.project_key(cwd) or "workspace"

    parts = []

    # 1) working memory — where we left off in THIS workspace
    meta, body = now_mod.read_now(vault, workspace)
    if body and _fresh(meta, lim["boot_now_max_age_days"]):
        body = body.strip()[: lim["boot_max_chars"]]
        parts.append(
            f"## Where we left off — workspace `{workspace}` "
            f"(saved {meta.get('updated', '?')}, by {meta.get('author', '?')})\n"
            f"{body}\n"
            f"(full page: {now_mod.now_path(vault, workspace)})"
        )

    # 2) today's briefing — the daily agenda mailbox, injected while fresh so
    # "o que tem pra hoje?" is answerable from context the session already has
    bmeta, bbody = now_mod.read_now(vault, now_mod.briefing_key(workspace))
    if bbody and _age_hours(bmeta) <= lim["briefing_max_age_hours"]:
        bbody = bbody.strip()[: lim["boot_max_chars"]]
        parts.append(
            f"## Today's briefing — workspace `{workspace}` "
            f"(saved {bmeta.get('updated', '?')})\n{bbody}"
        )

    # 3) long-term memory pointers — when a project hub shares this workspace's
    # name (the git-repo case). Content-inferred projects surface via recall.
    hub = vault / "wiki" / "projects" / f"{workspace}.md"
    n_pages = _count_pages(vault, workspace)
    if hub.is_file():
        parts.append(f"Project hub ({n_pages} wiki page(s) on `{workspace}`): {hub}")
    elif n_pages:
        parts.append(f"{n_pages} wiki page(s) mention `{workspace}` — `memex search` finds them.")

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
        '- save a fact:     memex remember "<one clear paragraph>"',
        "- save state:      pipe a short Markdown handoff to `memex handoff --stdin`",
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
