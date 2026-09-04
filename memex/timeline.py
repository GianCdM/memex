"""memex timeline — the compilation trail of a page (or a raw), ts-ordered.

Layer 2 of the progressive-disclosure search: given a page slug or a raw
filename, answer "what came BEFORE and AFTER it". Every applied merge writes
one line to `.memex/changelog.jsonl` (`{ts, page, kind, status, action,
source, raw}`) — uncapped, one row per page per raw — so filtering that file
by page (or raw basename) and sorting by ts IS the decision/debugging flow
around a point. No loader existed; this module is that loader.

LLM-free and stdlib-only (a leaf module: imports only canon/config/limits,
never synth/changes — the dependency direction is canon -> format, never
canon -> synth). Output is compact on purpose: timestamps, actions, sources
and raw basenames, never paths or bodies — the model escalates via `page`
(or Read) only for the entries it decides matter.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import canon as canon_mod
from . import config as config_mod
from . import limits as limits_mod


def _load_changelog(vault: Path) -> list[dict]:
    """Parse .memex/changelog.jsonl (one JSON row per applied change).

    File order is chronological by construction (append-only). A bad line is
    skipped, never fatal — a partial write must not blind the whole trail."""
    path = Path(vault) / ".memex" / "changelog.jsonl"
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _event(row: dict) -> dict:
    """The compact event the caller sees: core fields, missing -> None."""
    return {
        "ts": row.get("ts"),
        "ts_iso": (datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                   if isinstance(row.get("ts"), int) else None),
        "action": row.get("action"),
        "page": row.get("page"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "source": row.get("source"),
        "raw": row.get("raw"),
    }


def timeline(vault, *, page=None, raw=None, limit=None) -> dict:
    """The ordered compilation trail for one page slug OR one raw filename.

    Filter rows by page (or raw basename — tolerates `raw/<n>`,
    `.memex/raw/<n>` and bare names), sort by ts, collapse exact-consecutive
    duplicates (a re-apply writes identical rows), cap to the most recent
    `timeline_max_events`. The response carries the events plus aggregate
    counts; in raw mode the physical raw path is resolved for Read access."""
    vault = Path(vault)
    rows = _load_changelog(vault)
    if not rows or not (page or raw):
        return {"ok": True, "events": [], "counts": {"events": 0, "created": 0,
                 "updated": 0, "recurrences": 0}, "truncated": 0}

    raw_name = Path(raw).name if raw else None
    if page:
        hits = [r for r in rows if r.get("page") == page]
    else:
        hits = [r for r in rows if r.get("raw") == raw_name]

    return _build_out(vault, hits, page=page, raw_name=raw_name, limit=limit)


def project_timeline(vault, *, project, limit=None) -> dict:
    """The most recent merges across every page of one project.

    Joins changelog rows to index page records by slug and keeps those whose
    `project` matches. This is the "what has my brain been learning about X
    lately" view — the timeline primitive raised from a page to a project."""
    vault = Path(vault)
    rows = _load_changelog(vault)
    proj_pages = {p.get("slug"): p for p in canon_mod.load_index(vault).get("pages", [])
                  if (p.get("project") or "") == project}
    if not rows or not proj_pages:
        return {"ok": True, "project": project, "events": [],
                "counts": {"events": 0, "created": 0, "updated": 0,
                           "recurrences": 0}, "truncated": 0}
    hits = [r for r in rows if r.get("page") in proj_pages]
    out = _build_out(vault, hits, page=None, raw_name=None, limit=limit)
    out["project"] = project
    return out


def _build_out(vault, hits, *, page=None, raw_name=None, limit=None) -> dict:
    """Shared tail of every timeline flavor: dedup, ts-sort, cap, counts."""
    deduped = []
    for r in hits:
        if deduped and deduped[-1] == r:
            continue
        deduped.append(r)
    deduped.sort(key=lambda r: (r.get("ts") or 0))

    lim = limits_mod.load(vault)
    cap = int(limit) if limit else int(lim.get("timeline_max_events", 20))
    truncated = max(0, len(deduped) - cap)
    if truncated:
        deduped = deduped[-cap:]  # keep the most recent tail

    events = [_event(r) for r in deduped]
    created = sum(1 for e in events if e["action"] == "create")
    updated = sum(1 for e in events if e["action"] == "update")
    recurrences = len(hits) - len(deduped) + (1 if deduped else 0)

    out: dict = {
        "ok": True,
        "events": events,
        "counts": {"events": len(events), "created": created,
                   "updated": updated, "recurrences": recurrences},
        "truncated": truncated,
    }
    if page:
        for rec in canon_mod.load_index(vault).get("pages", []):
            if rec.get("slug") == page:
                out["section"] = rec.get("section")
                break
    elif raw_name:
        physical = canon_mod.raw_dir(vault) / raw_name
        if physical.is_file():
            out["raw_path"] = str(physical)
    return out


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1
    page = getattr(args, "slug", None)
    raw = getattr(args, "raw", None)
    project = getattr(args, "project", None)
    if not page and not raw and not project:
        print('usage: memex timeline <slug>   (or: --raw <name> / --project <slug>)')
        return 1
    if project:
        out = project_timeline(vault, project=project,
                               limit=getattr(args, "limit", None))
    else:
        out = timeline(vault, page=page, raw=raw,
                       limit=getattr(args, "limit", None))
    events = out.get("events") or []
    if not events:
        target = project or page or raw
        print(f"no changelog events for: {target}")
        return 0
    label = f"project {project}" if project else (f"page {page}" if page else f"raw {raw}")
    print(f"{len(events)} event(s) for {label}"
          + (f" (dropped {out['truncated']} older)" if out.get("truncated") else "")
          + "\n")
    for e in events:
        print(f"  {e['ts_iso'] or '?'}  {str(e['action']):8}  {e['page'] or '-'}"
              f"  [{e['source'] or '-'}]  {e['raw'] or '-'}")
    if out.get("raw_path"):
        print(f"\nraw: {out['raw_path']}")
    return 0
