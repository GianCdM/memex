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
from . import providers
from . import synth

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
    Catches facet-families (e.g. ifood-tech-pptx-*) that Jaccard alone misses
    because their tags/summaries diverge enough to sink the overlap ratio."""
    parts = [p for p in (slug or "").split("-") if p]
    if len(parts) < n or parts[0] in _GENERIC_SLUG_SEG:
        return None
    return tuple(parts[:n])


def _cluster(pages, threshold):
    """Union-find: pages cluster by Jaccard token overlap OR a shared slug prefix."""
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
    groups = {}
    for i in range(len(pages)):
        groups.setdefault(find(i), []).append(pages[i])
    return list(groups.values())


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text())
    except Exception:
        print(f"error: {vault} has no readable index (.memex/index.json).")
        return 1

    pages = idx.get("pages", [])
    threshold = getattr(args, "threshold", None) or 0.4
    clusters = [g for g in _cluster(pages, threshold) if len(g) > 1]

    if not clusters:
        print(f"nothing to consolidate (no near-duplicate clusters at threshold {threshold}).")
        return 0

    print(f"{len(clusters)} cluster(s) of near-duplicates (threshold {threshold}):")
    for g in clusters:
        print(f"  [{len(g)}] {', '.join(p['slug'] for p in g)}")

    if getattr(args, "dry_run", False):
        print("\n(dry-run — nothing changed. Re-run without --dry-run to merge, or tune --threshold.)")
        return 0

    vcfg = config_mod.load_vault(vault)
    name, kind, settings = config_mod.resolve_provider(getattr(args, "provider", None), vault_cfg=vcfg)
    model = getattr(args, "model_merge", None) or settings.get("model_merge")
    if not model:
        print(f"error: no merge model for provider '{name}' (set --model-merge or run `memex doctor`).")
        return 1
    print(f"\nmerging with {name}/{model}...")

    hist = vault / ".memex" / "history" / "gardening"
    changelog = vault / ".memex" / "changelog.jsonl"
    removed = set()

    for g in clusters:
        blocks = []
        for m in g:
            mp = vault / "wiki" / m["path"]
            _, body = synth._read_frontmatter(mp.read_text() if mp.exists() else "")
            blocks.append(f"## {m.get('title', m['slug'])}\n\n{body[:3000]}")
        try:
            merged = providers.complete(
                GARDEN_PROMPT.format(pages="\n\n---\n\n".join(blocks)),
                kind=kind, model=model, settings=settings)
        except providers.ProviderError as e:
            print(f"  cluster '{g[0]['slug']}': provider error: {e} — skipped")
            continue
        merged = synth._clean_body(merged)

        canon = min(g, key=lambda m: len(m.get("slug", "")))  # shortest slug = most general
        tier = max((m.get("tier", "silver") for m in g), key=lambda t: synth.TIER_RANK.get(t, 1))
        sources = list(dict.fromkeys(s for m in g for s in (m.get("sources") or [])))
        tags = list(dict.fromkeys(t for m in g for t in (m.get("tags") or [])))[:8]
        title = canon.get("title") or canon["slug"]

        (vault / "wiki" / canon["path"]).write_text(
            synth._render_page(title=title, tags=tags, tier=tier, sources=sources, body=merged))

        hist.mkdir(parents=True, exist_ok=True)
        for m in g:
            if m["slug"] == canon["slug"]:
                continue
            mp = vault / "wiki" / m["path"]
            if mp.exists():
                (hist / f"{int(time.time())}--{m['slug']}.md").write_text(mp.read_text())
                mp.unlink()
            removed.add(m["slug"])

        canon.update({"title": title, "tier": tier, "tags": tags, "sources": sources})
        with changelog.open("a") as ch:
            ch.write(json.dumps({
                "ts": int(time.time()), "page": canon["slug"], "tier": tier,
                "action": "garden-merge",
                "absorbed": [m["slug"] for m in g if m["slug"] != canon["slug"]],
            }) + "\n")
        print(f"  ✓ {len(g)} -> {canon['slug']}")

    idx["pages"] = [p for p in pages if p["slug"] not in removed]
    idx_path.write_text(json.dumps(idx, indent=2) + "\n")
    synth._write_index_md(vault, idx)
    print(f"\n✓ gardening done. {len(idx['pages'])} page(s) remain "
          f"({len(removed)} absorbed -> .memex/history/gardening/).")
    return 0


SUGGESTIONS_FILE = "_sugestoes.md"


def write_suggestions(vault, threshold=0.3) -> int:
    """Non-destructive: detect near-duplicate clusters and surface them as a
    gentle note INSIDE the wiki (the user merges in Obsidian if they agree, or
    ignores it). NEVER merges or deletes anything. Returns the cluster count.

    This is the automatic half of gardening — detection is safe to do silently;
    the semantic merge stays a human decision (Obsidian-style suggestion)."""
    vault = Path(vault)
    idx_path = vault / ".memex" / "index.json"
    note = vault / "wiki" / SUGGESTIONS_FILE
    try:
        idx = json.loads(idx_path.read_text())
    except Exception:
        return 0
    clusters = [g for g in _cluster(idx.get("pages", []), threshold) if len(g) > 1]
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
    note.write_text("\n".join(lines))
    return len(clusters)
