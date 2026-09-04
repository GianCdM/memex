"""memex page — read one wiki page's body under a character budget.

Layer 3 of the progressive-disclosure search: today the only escalation from
a search hit is "Read the whole file" — expensive when the model only needs
to confirm one fact. This module reads the canonical page body, capped, with
the total size and a hint on how to get the rest (tail mode / bigger
budget), so the model pays for detail only when it decides to.

Canonical confinement is enforced via `canon.canonical_path` — only pages
under wiki/topics|entities|decisions|projects with status current are
readable. LLM-free and stdlib-only (leaf module: imports only
canon/config/limits/format)."""

from __future__ import annotations

from pathlib import Path

from . import canon as canon_mod
from . import config as config_mod
from . import limits as limits_mod
from .format import read_frontmatter


def read_page(vault, *, slug=None, path=None, max_chars=None, mode="head") -> dict:
    """One page body under a budget, head (default) or tail slice.

    Resolve the record by slug (index lookup) or by wiki-relative path
    (`<section>/<slug>.md` stem), confine it canonically, split off the
    tool-owned frontmatter with format.read_frontmatter, and slice the body
    to `read_page_chars` (or the explicit max_chars). The response carries
    total vs returned size, a truncated flag and a hint naming the other
    mode — the caller escalates deliberately, never blindly."""
    vault = Path(vault)
    if mode not in ("head", "tail"):
        return {"ok": False, "error": f"mode must be 'head' or 'tail', got {mode!r}"}
    if not slug and not path:
        return {"ok": False, "error": "slug or path required"}

    record = None
    if slug:
        for rec in canon_mod.load_index(vault).get("pages", []):
            if rec.get("slug") == slug:
                record = rec
                break
    else:
        rel = str(path).strip()
        section = rel.split("/")[0] if "/" in rel else ""
        record = {"slug": Path(rel).stem, "section": section,
                  "path": rel, "status": "current"}

    physical = canon_mod.canonical_path(vault, record) if record else None
    if physical is None or not physical.is_file():
        target = slug or path
        return {"ok": False, "error": f"no canonical page for: {target}"}

    text = physical.read_text(encoding="utf-8", errors="ignore")
    meta, body = read_frontmatter(text)
    title = (meta.get("title") or "").strip().strip("\"'") or None

    lim = limits_mod.load(vault)
    budget = int(max_chars) if max_chars else int(lim.get("read_page_chars", 8000))
    total = len(body)
    truncated = total > budget
    body_slice = body[-budget:] if (mode == "tail" and truncated) else body[:budget]

    out = {
        "ok": True,
        "slug": record.get("slug"),
        "section": record.get("section"),
        "title": title,
        "path": str(physical),
        "total_chars": total,
        "returned_chars": len(body_slice),
        "mode": mode,
        "truncated": truncated,
    }
    if truncated:
        out["hint"] = (f"page is {total} chars; returned {len(body_slice)} ({mode}). "
                       f"Use mode='tail' or raise max_chars.")
    out["body"] = body_slice
    return out


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1
    slug = getattr(args, "slug", None)
    path = getattr(args, "path", None)
    if not slug and not path:
        print('usage: memex page <slug> [--chars N] [--tail]   (or: --path <section>/<slug>.md)')
        return 1
    out = read_page(vault, slug=slug, path=path,
                    max_chars=getattr(args, "chars", None),
                    mode="tail" if getattr(args, "tail", False) else "head")
    if not out.get("ok"):
        print(out.get("error", "read failed"))
        return 1
    print(f"# {out.get('title') or out.get('slug')}  ({out['returned_chars']}/{out['total_chars']} chars, {out['mode']})\n")
    print(out["body"])
    if out.get("hint"):
        print(f"\n({out['hint']})")
    return 0
