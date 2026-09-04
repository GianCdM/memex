"""memex views — regenerate machine-owned navigation catalogs.

Project hubs are now CANONICAL wiki pages (wiki/projects/<project>.md) with
frontmatter + a record in index.json, so they are visible in Obsidian and
recallable like any other wiki page. They are still regenerated deterministically
from index.json on every synthesis — no LLM — but they live inside the wiki.

Derived catalogs stay under .memex/views/ (a dot-dir, hidden from Obsidian):
  .memex/views/brain-index.md        — catalog of every wiki page, by section.
  .memex/views/projects-index.md     — index of the per-project hub pages.

Projects are semantic (initiative/area/repo) — many workspaces can feed one
project, and one generic workspace can feed many projects.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import canon as canon_mod
from .format import read_frontmatter


def write_views(vault: Path, index: dict) -> None:
    """Regenerate the project hubs (canonical, in wiki/projects/) + the derived
    catalogs (.memex/views/). Hubs are registered in index.json so they become
    canonical pages (recallable, in the graph).

    `index` is mutated in place (hub records merged into index["pages"]) so the
    caller's in-memory index reflects the hubs, and persisted via canon.write_index.
    """
    vault = Path(vault)
    views_dir = vault / ".memex" / "views"
    views_dir.mkdir(parents=True, exist_ok=True)
    pages = index.get("pages", []) if isinstance(index, dict) else []
    hubs = _write_project_hubs(vault, pages)
    _write_brain_index(views_dir, pages, hubs)
    _write_projects_index(views_dir, hubs)
    # Merge hubs into the caller's index (replace stale hub records) + persist.
    if isinstance(index, dict):
        kept = [p for p in index.get("pages", []) if p.get("kind") != "hub"]
        index["pages"] = kept + hubs
        canon_mod.write_index(vault, index["pages"])


def _frontmatter(d: dict) -> str:
    """Minimal frontmatter block for a hub page, matching the tool's parser
    (`read_frontmatter` does raw `key: value`, so only title carries quotes)."""
    title = str(d.get("title") or "").replace('"', "'")
    lines = ["---", f'title: "{title}"']
    for key in ("kind", "status", "project"):
        if d.get(key):
            lines.append(f"{key}: {d[key]}")
    lines.append("---")
    return "\n".join(lines)


def _is_generated_hub(vault: Path, fp: Path) -> bool:
    """True if a wiki/projects file is a memex-generated hub (has kind: hub in
    frontmatter) — such files are safe to clear/regenerate. Hand-authored pages
    (no frontmatter or different kind) are never touched."""
    try:
        meta, _ = read_frontmatter(fp.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return False
    return meta.get("kind") == "hub"


def _recent_changelog_by_project(vault: Path, by_proj: dict, n: int = 5) -> dict:
    """Slug -> {project: [most recent N events]} for hub 'Últimos eventos'.

    One shared pass over changelog.jsonl (cheap even at 4k+ rows): rows whose
    page belongs to one of the hub projects keep their latest timestamps.
    Missing file / bad rows are silently skipped — hubs are derived views and
    a broken changelog must never break write_views."""
    slugs_to_proj = {p["slug"]: proj
                     for proj, plist in by_proj.items() for p in plist}
    out: dict = {}
    try:
        lines = (vault / ".memex" / "changelog.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = rec.get("page")
        proj = slugs_to_proj.get(slug)
        if not proj or not isinstance(rec.get("ts"), int):
            continue
        from datetime import datetime
        ev = {"page": slug, "action": rec.get("action"),
              "ts": rec["ts"],
              "ts_iso": datetime.fromtimestamp(rec["ts"]).strftime("%Y-%m-%d %H:%M")}
        bucket = out.setdefault(proj, [])
        bucket.append(ev)
    for proj in out:
        out[proj] = sorted(out[proj], key=lambda e: -e["ts"])[:n]
    return out


def _write_project_hubs(vault: Path, pages: list[dict]) -> list[dict]:
    """Generate one canonical hub per project in wiki/projects/<slug>.md.

    A hub aggregates the wiki pages carrying that `project` (arch + session +
    doc), with frontmatter + a record for index.json. Hubs themselves (kind: hub)
    are excluded from the grouping so a hub never lists itself. Stale generated
    hubs (kind: hub) are cleared before regenerating; hand-authored pages in
    wiki/projects/ are preserved.
    """
    by_proj = defaultdict(list)
    for p in pages:
        if p.get("project") and p.get("kind") != "hub":
            by_proj[p["project"]].append(p)

    hubs_dir = vault / "wiki" / "projects"
    hubs_dir.mkdir(parents=True, exist_ok=True)
    # clear ONLY generated hubs (kind: hub) — never hand-authored files
    for stale in hubs_dir.glob("*.md"):
        if _is_generated_hub(vault, stale):
            try:
                stale.unlink()
            except OSError:
                pass

    def _kind(p):
        srcs = [str(s) for s in (p.get("sources") or [])]
        if any(s.startswith("analyze:") for s in srcs):
            return "arch"
        if any(s.startswith("doc:") for s in srcs):
            return "doc"
        return "session"

    # one shared changelog read for ALL hubs: slug -> most recent N events
    recent_events = _recent_changelog_by_project(vault, by_proj)

    hub_records = []
    for proj in sorted(by_proj):
        plist = by_proj[proj]
        buckets = {"arch": [], "session": [], "doc": []}
        for p in plist:
            buckets[_kind(p)].append(p)
        slug = proj.lower().replace(" ", "-").replace("/", "-")
        body = [f"# {proj}", "",
                f"*Project hub — {len(plist)} page(s), auto-generated by memex.*", ""]
        for key, label, emoji in [("arch", "Arquitetura", "🏛️"),
                                  ("session", "Sessões", "💬"),
                                  ("doc", "Docs", "📄")]:
            bucket = sorted(buckets[key], key=lambda x: x["slug"])
            if not bucket:
                continue
            body.append(f"## {emoji} {label}")
            for p in bucket:
                body.append(f"- [[{p['slug']}]] — {p.get('summary') or ''}")
            body.append("")
        events = recent_events.get(proj) or []
        if events:
            body.append("## 🕐 Últimos eventos")
            for e in events:
                body.append(f"- {e['ts_iso']} — [[{e['page']}]] ({e['action']})")
            body.append("")
        fm = _frontmatter({"title": proj, "kind": "hub", "status": "current",
                           "project": proj})
        (hubs_dir / f"{slug}.md").write_text(
            fm + "\n\n" + "\n".join(body).rstrip() + "\n", encoding="utf-8")
        hub_records.append({
            "slug": slug, "title": proj, "section": "projects", "kind": "hub",
            "status": "current", "tags": [], "sources": [], "project": proj,
            "summary": f"Project hub — {len(plist)} page(s)",
            "path": f"projects/{slug}.md",
        })
    return hub_records


def _write_brain_index(views_dir: Path, pages: list[dict], hubs: list[dict]) -> None:
    """The brain catalog: one line per canonical page (including hubs), grouped
    by wiki section. Stays a derived catalog under .memex/views/."""
    sections = {"topics": [], "entities": [], "decisions": [], "projects": []}
    for p in pages:
        sections.setdefault(p.get("section", "topics"), []).append(p)
    sections["projects"] = hubs or sections["projects"]
    lines = ["# Brain index", "", "Navigable catalog of wiki pages.", ""]
    for sec, title in [("topics", "Topics"), ("entities", "Entities"),
                       ("decisions", "Decisions"), ("projects", "Projects")]:
        lines.append(f"## {title}")
        for p in sorted(sections.get(sec, []), key=lambda x: x["slug"]):
            lines.append(f"- [[{p['slug']}]] — {p.get('summary', '')}")
        lines.append("")
    (views_dir / "brain-index.md").write_text("\n".join(lines), encoding="utf-8")


def _write_projects_index(views_dir: Path, hubs: list[dict]) -> None:
    """The projects catalog (derived): one line per hub with page count."""
    idx_lines = ["# Projects", "",
                 "One hub per project/initiative — each ties together "
                 "sessions · docs · architecture.", ""]
    for hub in sorted(hubs, key=lambda x: x["slug"]):
        idx_lines.append(f"- [[{hub['slug']}]] — {hub.get('summary', '')}")
    (views_dir / "projects-index.md").write_text("\n".join(idx_lines) + "\n",
                                                 encoding="utf-8")
