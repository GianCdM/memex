"""memex relink — retroactively connect orphan pages to the graph.

The propose model sometimes returns `related=[]` — page gets written without any
[[wikilinks]] and stays orphan in the Obsidian graph. This tool sweeps the vault,
finds orphans (0 incoming AND 0 outgoing links), scores the rest of the brain
by lexical similarity (tags + title + project overlap), and appends a short
`## Relacionado` section with 3-5 wikilinks.

Non-destructive: only ADDS a section, never touches existing content.
LLM-free: pure code, runs in seconds.

Run any time: `memex relink --vault ~/memex` (dry-run: --dry-run).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from . import canon as canon_mod
from . import changes as changes_mod
from . import config as config_mod
from . import synth


RELATED_SECTION_HEADER = "## Relacionado"
RELATED_MARKER = "<!-- memex-relink -->"


def _extract_links(body: str) -> set[str]:
    """All [[wikilink]] slugs referenced in the body (case-insensitive, kebab-normalized)."""
    out = set()
    for m in re.finditer(r"\[\[([^\]|#]+)", body or ""):
        t = re.sub(r"[^a-z0-9-]+", "-", m.group(1).strip().lower()).strip("-")
        if t:
            out.add(t)
    return out


def _build_graph(vault: Path, pages: list[dict]):
    """Return (outgoing, incoming) — slug -> {linked slugs}."""
    outgoing = defaultdict(set)
    incoming = defaultdict(set)
    all_slugs = {p["slug"] for p in pages}
    for p in pages:
        slug = p["slug"]
        f = vault / "wiki" / p["path"]
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        _, body = synth._read_frontmatter(text)
        for target in _extract_links(body):
            if target != slug and target in all_slugs:
                outgoing[slug].add(target)
                incoming[target].add(slug)
    return outgoing, incoming, all_slugs


def _score_lexical(page: dict, all_pages: list[dict]) -> list[tuple[float, str]]:
    """Rank pages by token overlap on {title, tags, project, slug}, with
    same-project and same-section boosts. Returns full ranked list so the
    caller can fuse with a semantic pass."""
    query = synth._tokens(page.get("title"), page.get("slug"), page.get("project"),
                          " ".join(page.get("tags") or []))
    if not query:
        return []
    scored = []
    for p in all_pages:
        if p["slug"] == page["slug"]:
            continue
        target = synth._tokens(p.get("title"), p["slug"], p.get("project"),
                               " ".join(p.get("tags") or []))
        if not target:
            continue
        overlap = len(query & target)
        if overlap < 1:
            continue
        same_project = 1 if page.get("project") and p.get("project") == page.get("project") else 0
        same_section = 1 if p.get("section") == page.get("section") else 0
        score = overlap + same_project * 3 + same_section * 0.5
        scored.append((score, p["slug"]))
    scored.sort(reverse=True, key=lambda x: x[0])
    return scored


def _score_semantic(page: dict, vecs_by_slug: dict) -> list[tuple[float, str]]:
    """Rank other pages by cosine similarity to this page's embedding.
    Assumes vectors are L2-normalized (embed.py normalizes at index time),
    so dot product == cosine.

    Returns [(score, slug), ...] best-first, restricted to pages whose vector
    has the same dimension as this page's (mixed-model corpus safety)."""
    slug = page["slug"]
    qv = vecs_by_slug.get(slug)
    if not qv:
        return []
    qdim = len(qv)
    scored = []
    for other, v in vecs_by_slug.items():
        if other == slug or len(v) != qdim:
            continue
        cos = sum(x * y for x, y in zip(qv, v))
        scored.append((cos, other))
    scored.sort(reverse=True, key=lambda x: x[0])
    return scored


def _rrf_fuse(a: list[tuple[float, str]], b: list[tuple[float, str]],
              k: int = 60, top: int = 4) -> list[str]:
    """Reciprocal Rank Fusion of two ranked lists — same idea recall uses.
    Score(slug) = Σ 1/(k + rank_in_list) across every list where it appears.
    A slug that scores well in BOTH the lexical and semantic view rises to
    the top. Returns just the top-N slugs."""
    fused: dict[str, float] = {}
    for r, (_, slug) in enumerate(a):
        fused[slug] = fused.get(slug, 0.0) + 1.0 / (k + r + 1)
    for r, (_, slug) in enumerate(b):
        fused[slug] = fused.get(slug, 0.0) + 1.0 / (k + r + 1)
    out = sorted(fused.items(), key=lambda x: -x[1])
    return [s for s, _ in out[:top]]


def _score_candidates(page: dict, all_pages: list[dict],
                      vecs_by_slug: dict | None = None,
                      k: int = 4) -> list[str]:
    """Top-k related slugs. When embeddings are available, fuse lexical +
    semantic via RRF — that catches cross-language cousins the lexical
    scorer misses (`domínio` ↔ `domain`). Falls back to lexical-only
    when embeddings aren't indexed."""
    lex = _score_lexical(page, all_pages)
    if vecs_by_slug:
        sem = _score_semantic(page, vecs_by_slug)
        if sem:
            # Trim each list before fusion so we don't get drowned by weak
            # semantic matches on a huge brain — top ~4x the target is enough.
            pool = max(k * 4, 20)
            return _rrf_fuse(lex[:pool], sem[:pool], top=k)
    return [s for _, s in lex[:k]]


def _related_section_text(page_path: Path, related_slugs: list[str]) -> str | None:
    """The page text with a `## Relacionado` block appended (or refreshed).

    READ-ONLY: returns the full new text so the caller can route it through the
    promoter as a ChangeSet's proposed body. Idempotent: if the marker is
    already there, replaces the previous list. Returns None when nothing would
    change (no slugs, or the exact same block is already present)."""
    if not related_slugs:
        return None
    text = page_path.read_text(encoding="utf-8")
    section = (
        f"\n\n{RELATED_SECTION_HEADER} {RELATED_MARKER}\n"
        + "\n".join(f"- [[{s}]]" for s in related_slugs)
        + "\n"
    )
    # If we already inserted a relink block, replace it instead of stacking.
    pattern = re.compile(
        rf"\n*{re.escape(RELATED_SECTION_HEADER)} {re.escape(RELATED_MARKER)}.*?(?=\n##\s|\Z)",
        re.DOTALL,
    )
    if pattern.search(text):
        new_text = pattern.sub(section, text).rstrip() + "\n"
    else:
        new_text = text.rstrip() + section
    if new_text == text:
        return None
    return new_text


def _has_relink_marker(page_path: Path) -> bool:
    """True when the page already carries a `<!-- memex-relink -->` block —
    used to detect prior-relink pages when running --refresh."""
    try:
        return RELATED_MARKER in page_path.read_text(encoding="utf-8")
    except OSError:
        return False


def run(args) -> int:
    vault_arg = getattr(args, "vault", None)
    vault = config_mod.resolve_vault(vault_arg)
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1

    dry_run = getattr(args, "dry_run", False)
    refresh = getattr(args, "refresh", False)
    do_all = getattr(args, "all", False)
    min_links = getattr(args, "min_links", 2)
    k = getattr(args, "top_k", 4)

    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: can't read index: {e}")
        return 1

    pages = canon_mod.canonical_pages(vault, idx)
    if not pages:
        print("empty brain — nothing to relink.")
        return 0

    outgoing, incoming, all_slugs = _build_graph(vault, pages)
    total_links_before = sum(len(v) for v in outgoing.values())

    # Optional semantic scoring: if the vault has embeddings, use them to
    # find cross-language cousins the lexical scorer would miss (e.g. an EN
    # page and its PT sibling about the same concept). Silent fallback on
    # any load error.
    vecs_by_slug = {}
    try:
        from . import recall as recall_mod
        vecs_by_slug, _meta = recall_mod._load_embeddings(vault)
    except Exception:
        vecs_by_slug = {}

    # Target selection — three modes, ordered by scope:
    #   default   → orphans only (0 in + 0 out)
    #   --all     → every page with < min_links connections (orphans + dead-ends)
    #   --refresh → every page that already has a relink block, PLUS the current
    #               targets of the default/--all rule. Use this after upgrading
    #               the scorer (e.g. turning embeddings on for the first time)
    #               so pages relinked with the old scorer get the new candidates.
    targets = []
    for p in pages:
        s = p["slug"]
        conns = len(outgoing.get(s, set())) + len(incoming.get(s, set()))
        already_relinked = refresh and _has_relink_marker(vault / "wiki" / p["path"])
        if already_relinked:
            targets.append(p)
        elif do_all and conns < min_links:
            targets.append(p)
        elif not do_all and conns == 0:
            targets.append(p)

    if refresh:
        mode_label = "refresh (all previously-relinked + current gaps)"
    elif do_all:
        mode_label = f"< {min_links} links"
    else:
        mode_label = "orphans"

    scorer_label = "lexical + semantic (RRF)" if vecs_by_slug else "lexical-only"
    print(f"vault: {vault}")
    print(f"  pages: {len(pages)}  |  links: {total_links_before}")
    print(f"  scorer: {scorer_label}")
    print(f"  targets ({mode_label}): {len(targets)}")
    if dry_run:
        print("  (dry-run — nothing will be written)")

    modified = 0
    linked_total = 0
    for p in targets:
        candidates = _score_candidates(p, pages, vecs_by_slug=vecs_by_slug, k=k)
        if not candidates:
            continue
        if dry_run:
            # read-only: still exercise the section-builder so the dry-run
            # output reflects exactly what a real run would touch
            _related_section_text(vault / "wiki" / p["path"], candidates)
            print(f"  [{p['slug']}] would link to: {', '.join(candidates)}")
        else:
            page_path = vault / "wiki" / p["path"]
            new_text = _related_section_text(page_path, candidates)
            if new_text is None:
                continue
            # Route the mutation through the promoter — NO direct wiki/ write.
            # relink is deterministic (no LLM) and claim-free, so the repair
            # ChangeSet seeds a supported/auto_apply verification and the
            # promoter renders the page with the Related section appended.
            _, new_body = synth._read_frontmatter(new_text)
            change = changes_mod.new_changeset(
                operation="repair",
                classification={"section": p["section"], "slug": p["slug"],
                                "title": p["title"], "project": p.get("project")},
                source={"kind": "relink"},
                target={"slug": p["slug"],
                        "expected_page_sha256": canon_mod.page_body_hash(
                            page_path.read_text(encoding="utf-8"))},
                claims=[],
                proposed_body=new_body,
                risk="low",
                reason="relink: add justified [[wikilinks]]",
            )
            change["verification"] = {"outcome": "supported", "route": "auto_apply"}
            changes_mod.save_changeset(vault, change)
            result = changes_mod.apply_changeset(vault, change["id"])
            if result.get("state") == "applied":
                modified += 1
                linked_total += len(candidates)
                # also update outgoing so the graph state stays consistent
                # (in case a later target could benefit from these new edges)
                for c in candidates:
                    outgoing[p["slug"]].add(c)
                    incoming[c].add(p["slug"])
            elif result.get("state") == "stale":
                print(f"  [{p['slug']}] skipped: page changed while relinking "
                      f"(ChangeSet {change['id']} marked stale)")
            else:
                print(f"  [{p['slug']}] ChangeSet {change['id']} parked "
                      f"({result.get('state')}: {result.get('reason') or 'review required'})")
    if dry_run:
        return 0

    # Rebuild changelog entry so the audit trail records the sweep
    changelog = vault / ".memex" / "changelog.jsonl"
    import time
    with changelog.open("a", encoding="utf-8") as ch:
        ch.write(json.dumps({
            "ts": int(time.time()), "action": "relink",
            "modified": modified, "links_added": linked_total,
        }) + "\n")
    print(f"\n✓ relink done. Modified {modified} page(s), added ~{linked_total} link(s).")
    total_after = total_links_before + linked_total
    print(f"  graph: {total_links_before} → {total_after} links (+{linked_total})")
    return 0
