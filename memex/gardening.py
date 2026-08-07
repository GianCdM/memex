"""memex gardening — consolidate near-duplicate wiki pages.

The propose step can over-fragment: many sessions about the same topic become
many near-duplicate pages (e.g. 22 "prism-session-reviewer-*" variants). Gardening
clusters pages by lexical overlap (slug + title + tags + summary) and LLM-merges
each cluster into ONE coherent page, archiving the absorbed pages to
.memex/history/gardening/ (recoverable, never hard-lost).

One LLM merge call per cluster (cheap: N pages -> 1 call). Stdlib only.
Use --dry-run to preview clusters, --threshold to tune grouping (default 0.4).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config as config_mod
from . import limits as limits_mod
from . import providers
from . import synth
from . import views as views_mod

GARDEN_PROMPT = """You are consolidating several wiki pages that are ALL about the same topic into ONE coherent page.

Merge them: keep every durable fact, organize under clear `## headings`, and remove repetition / near-duplicate sections. Output ONLY the Markdown body — NO YAML frontmatter, NO preamble or meta-commentary, start directly with a `## heading`. Keep the content's own language (Portuguese / English as written).

PAGES TO MERGE (each starts with its `## title`):
{pages}
"""


def _tok(s):
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 3}


def _page_tokens(p):
    return _tok(
        p.get("slug", "") + " " + p.get("title", "") + " "
        + " ".join(p.get("tags", []) or []) + " " + (p.get("summary", "") or "")
    )


_GENERIC_SLUG_SEG = {"note", "untitled", "misc", "draft", "doc"}


def _slug_prefix(slug, n=3):
    """First n slug segments as a grouping key — None if too short or generic.
    Catches facet-families (e.g. design-system-pptx-*) that Jaccard alone misses
    because their tags/summaries diverge enough to sink the overlap ratio."""
    parts = [p for p in (slug or "").split("-") if p]
    if len(parts) < n or parts[0] in _GENERIC_SLUG_SEG:
        return None
    return tuple(parts[:n])


def _cluster(pages, threshold, vault=None, semantic_threshold=0.85):
    """Union-find: pages cluster when ANY of these signals fires:
      - Jaccard token overlap >= threshold (lexical)
      - shared slug prefix (facet families, e.g. `design-system-pptx-*`)
      - cosine similarity >= semantic_threshold on stored embeddings (semantic)

    The semantic pass catches cross-language duplicates that lexical misses
    (e.g. `merchant-onboarding-guide.md` in EN and `guia-onboarding-parceiro.md`
    in PT about the same topic). It's a strict-threshold add-on: only very
    similar pages (0.85+ cosine on L2-normalized vectors) get merged, so we
    don't over-cluster.

    Silently skipped when vault is None or embeddings aren't indexed —
    gardening keeps working as before (lexical + slug prefix) on any vault
    that never turned semantic recall on.
    """
    toks = [_page_tokens(p) for p in pages]
    prefixes = [_slug_prefix(p.get("slug", "")) for p in pages]
    parent = list(range(len(pages)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            a, b = toks[i], toks[j]
            jac = (len(a & b) / len(a | b)) if (a and b) else 0.0
            same_prefix = prefixes[i] is not None and prefixes[i] == prefixes[j]
            if jac >= threshold or same_prefix:
                parent[find(i)] = find(j)

    # Optional semantic cross-check: pull embeddings, run pairwise cosine on
    # the top-K neighbors of each page. Local-only, no network calls — this
    # just reads the precomputed vectors that `memex embed` produced.
    if vault is not None:
        try:
            from . import recall as recall_mod
            vecs_by_slug, _meta = recall_mod._load_embeddings(vault)
        except Exception:
            vecs_by_slug = {}
        if vecs_by_slug:
            # Same-dim guard: mixed-model corpora would poison cosine scores.
            dims = {len(v) for v in vecs_by_slug.values()}
            if len(dims) == 1:
                slug_to_i = {p["slug"]: i for i, p in enumerate(pages) if p.get("slug") in vecs_by_slug}
                slugs = list(slug_to_i.keys())
                n = len(slugs)
                # O(n^2) is fine at wiki scale (~1k pages -> ~500k pairs, all
                # cheap dot products since vectors are pre-normalized).
                for a_idx in range(n):
                    sa = slugs[a_idx]
                    va = vecs_by_slug[sa]
                    i = slug_to_i[sa]
                    for b_idx in range(a_idx + 1, n):
                        sb = slugs[b_idx]
                        vb = vecs_by_slug[sb]
                        cos = sum(x * y for x, y in zip(va, vb))
                        if cos >= semantic_threshold:
                            j = slug_to_i[sb]
                            parent[find(i)] = find(j)

    groups = {}
    for i in range(len(pages)):
        groups.setdefault(find(i), []).append(pages[i])
    return list(groups.values())


def run(args) -> int:
    """CLI entry (`memex tidy`, legacy alias `memex gardening`)."""
    vault = Path(args.vault).expanduser().resolve()
    return consolidate(
        vault,
        provider=getattr(args, "provider", None),
        threshold=getattr(args, "threshold", None),
        model_merge=getattr(args, "model_merge", None),
        dry_run=getattr(args, "dry_run", False),
    )


def consolidate(vault, provider=None, threshold=None, model_merge=None, dry_run=False) -> int:
    """Cluster near-duplicate pages and LLM-merge each cluster into one page,
    archiving the absorbed pages recoverably. Called automatically by reflect
    on a cadence (auto-tidy) and manually via `memex tidy`.

    Returns 0 = done, 1 = config error, 2 = provider looks down (circuit
    breaker), 3 = vault busy (a synth/tidy holds the per-vault lock).

    Holds the SAME per-vault lock as synth: index.json and the wiki pages are
    shared state, and a tidy racing an in-flight synth would produce ghost
    index entries pointing at deleted files. Busy -> skip, caller retries."""
    vault = Path(vault)
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1
    if dry_run:  # read-only preview — no lock needed
        return _consolidate_impl(vault, provider, threshold, model_merge, True)
    lock = synth._acquire_lock(vault)
    if lock is None:
        print("vault is busy (a synth/tidy is running) — skipping tidy this time.")
        return 3
    try:
        return _consolidate_impl(vault, provider, threshold, model_merge, False)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _consolidate_impl(vault, provider, threshold, model_merge, dry_run) -> int:
    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        print(f"error: {vault} has no readable index (.memex/index.json).")
        return 1

    lim = limits_mod.load(vault)
    pages = idx.get("pages", [])
    if threshold is None:  # `or` would silently discard an explicit 0.0
        threshold = lim["garden_merge_threshold"]
    clusters = [g for g in _cluster(
        pages, threshold, vault=vault,
        semantic_threshold=lim.get("garden_semantic_threshold", 0.85),
    ) if len(g) > 1]

    if not clusters:
        print(f"nothing to consolidate (no near-duplicate clusters at threshold {threshold}).")
        return 0

    print(f"{len(clusters)} cluster(s) of near-duplicates (threshold {threshold}):")
    for g in clusters:
        print(f"  [{len(g)}] {', '.join(p['slug'] for p in g)}")

    if dry_run:
        print("\n(dry-run — nothing changed. Re-run without --dry-run to merge, or tune --threshold.)")
        return 0

    vcfg = config_mod.load_vault(vault)
    name, kind, settings = config_mod.resolve_provider(provider, vault_cfg=vcfg)
    model = model_merge or settings.get("model_merge")
    if not model:
        print(f"error: no merge model for provider '{name}' (set --model-merge or run `memex doctor`).")
        return 1
    print(f"\nmerging with {name}/{model}...")

    hist = vault / ".memex" / "history" / "gardening"
    changelog = vault / ".memex" / "changelog.jsonl"
    removed = set()
    consecutive_errors = 0
    rc = 0

    for g in clusters:
        blocks = []
        for m in g:
            mp = vault / "wiki" / m["path"]
            _, body = synth._read_frontmatter(mp.read_text(encoding="utf-8") if mp.exists() else "")
            blocks.append(f"## {m.get('title', m['slug'])}\n\n{body[:lim['garden_merge_chars']]}")
        try:
            merged = providers.complete(
                GARDEN_PROMPT.format(pages="\n\n---\n\n".join(blocks)),
                kind=kind, model=model, settings=settings)
        except providers.ProviderError as e:
            print(f"  cluster '{g[0]['slug']}': provider error: {e} — skipped")
            consecutive_errors += 1
            if consecutive_errors >= 3:  # provider likely down — this runs unattended
                print("  3 provider errors in a row — stopping tidy (retry on the next reflect).")
                rc = 2
                break
            continue
        consecutive_errors = 0
        merged = synth._clean_body(merged)

        canon = min(g, key=lambda m: len(m.get("slug", "")))  # shortest slug = most general
        sources = list(dict.fromkeys(s for m in g for s in (m.get("sources") or [])))
        tags = list(dict.fromkeys(t for m in g for t in (m.get("tags") or [])))[:8]
        title = canon.get("title") or canon["slug"]

        # archive EVERY member — including canon, which is about to be
        # overwritten by a merge that saw truncated bodies. "Recoverable,
        # never hard-lost" must hold for the canonical page too.
        hist.mkdir(parents=True, exist_ok=True)
        for m in g:
            mp = vault / "wiki" / m["path"]
            if mp.exists():
                (hist / f"{int(time.time())}--{m['slug']}.md").write_text(
                    mp.read_text(encoding="utf-8"), encoding="utf-8")
                if m["slug"] != canon["slug"]:
                    mp.unlink()
            if m["slug"] != canon["slug"]:
                removed.add(m["slug"])

        (vault / "wiki" / canon["path"]).write_text(
            synth._render_page(title=title, tags=tags, kind="merged", status="current",
                               sources=sources, body=merged,
                               project=canon.get("project")), encoding="utf-8")

        canon.update({"title": title, "kind": "merged", "status": "current",
                      "tags": tags, "sources": sources})
        with changelog.open("a", encoding="utf-8") as ch:
            ch.write(json.dumps({
                "ts": int(time.time()), "page": canon["slug"], "kind": "merged",
                "status": "current",
                "action": "garden-merge",
                "absorbed": [m["slug"] for m in g if m["slug"] != canon["slug"]],
            }) + "\n")
        # persist the index PER CLUSTER: if this unattended process dies
        # mid-run, the index never lists pages whose files are already gone
        idx["pages"] = [p for p in pages if p["slug"] not in removed]
        idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        print(f"  ✓ {len(g)} -> {canon['slug']}")

    idx["pages"] = [p for p in pages if p["slug"] not in removed]
    idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    views_mod.write_views(vault, idx)
    print(f"\n✓ tidy done. {len(idx['pages'])} page(s) remain "
          f"({len(removed)} absorbed -> .memex/history/gardening/).")
    if removed:
        try:
            from . import vault as vault_mod
            vault_mod.log_append(vault, f"tidy: {len(removed)} near-duplicate page(s) "
                                        f"absorbed into {len(clusters)} page(s)")
        except Exception:
            pass
    return rc


SUGGESTIONS_FILE = "merge-suggestions.md"


def write_suggestions(vault, threshold=None) -> int:
    """Non-destructive: detect near-duplicate clusters and surface them as an
    audit report in .memex/audit/ (the user merges in Obsidian if they agree,
    or ignores it). NEVER merges or deletes anything. Returns the cluster count.

    This is the automatic half of gardening — detection is safe to do silently;
    the semantic merge stays a human decision (Obsidian-style suggestion)."""
    vault = Path(vault)
    lim = limits_mod.load(vault)
    if threshold is None:
        threshold = lim["garden_suggest_threshold"]
    idx_path = vault / ".memex" / "index.json"
    note = vault / ".memex" / "audit" / SUGGESTIONS_FILE
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    clusters = [g for g in _cluster(
        idx.get("pages", []), threshold, vault=vault,
        semantic_threshold=lim.get("garden_semantic_threshold", 0.85),
    ) if len(g) > 1]
    if not clusters:
        if note.exists():
            note.unlink()  # nothing to suggest -> the note disappears on its own
        return 0

    lines = [
        "# Sugestões de organização", "",
        "> Gerado automaticamente pelo memex. As páginas abaixo parecem tratar do",
        "> **mesmo assunto**. Se concordar, junte-as numa só (mova o conteúdo para a",
        "> página principal e apague as outras). Se não, **ignore** — nada quebra, e",
        "> esta nota some sozinha quando não houver mais sugestões.", "",
    ]
    for g in sorted(clusters, key=len, reverse=True):
        canon = min(g, key=lambda m: len(m.get("slug", "")))
        others = [m for m in g if m["slug"] != canon["slug"]]
        lines.append(f"## {canon.get('title') or canon['slug']}  ({len(g)} páginas)")
        lines.append(f"- principal sugerida: [[{canon['slug']}]]")
        lines.append("- parecem irmãs: " + ", ".join(f"[[{m['slug']}]]" for m in others))
        lines.append("")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("\n".join(lines), encoding="utf-8")
    return len(clusters)
