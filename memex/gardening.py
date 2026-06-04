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


def _cluster(pages, threshold):
    """Union-find clustering by Jaccard overlap of page tokens."""
    toks = [_page_tokens(p) for p in pages]
    parent = list(range(len(pages)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            a, b = toks[i], toks[j]
            if a and b and len(a & b) / len(a | b) >= threshold:
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
