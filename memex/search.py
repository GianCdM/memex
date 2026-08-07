"""memex search — query the brain from anywhere (human or agent).

Same hybrid ranker as `recall` (lexical + optional semantic embeddings fused
via RRF), but interactive: no session dedup, relaxed lexical gates, and paths
always printed so the caller can open/Read the page. This is the verb the
SKILL teaches Claude to use mid-session, and the one you use in a terminal.

Semantic layer is auto-detected: if the vault has precomputed embeddings AND
a provider is configured (`memex config set provider.embeddings.*`), search
queries are embedded and fused with the lexical hits — matches survive
across languages (e.g. "domínio" hits pages titled "domain-*"). Otherwise it
falls back to lexical-only, silently.
"""

from __future__ import annotations

import json

from . import canon as canon_mod
from . import config as config_mod
from . import limits as limits_mod
from . import recall as recall_mod


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1
    query = " ".join(getattr(args, "terms", None) or []).strip()
    if not query:
        print('usage: memex search "<terms>"')
        return 1
    try:
        index = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("the brain has no index yet (run `memex init` / `memex reflect`).")
        return 1

    lim = dict(limits_mod.load(vault))
    lim["retrieve_min_overlap"] = 1   # search is exploratory — relax the gates
    lim["retrieve_min_score"] = 0.0
    scored = recall_mod.hybrid_rank(
        canon_mod.canonical_pages(vault, index), query, lim, vault,
        min_tokens=1, log_prefix="memex search",
    )
    if not scored:
        print(f"no pages matched: {query}")
        print(f"(catalog: {vault / '.memex' / 'views' / 'brain-index.md'})")
        return 0

    limit = getattr(args, "limit", None) or 10
    print(f"top {min(limit, len(scored))} of {len(scored)} match(es) for: {query}\n")
    for score, p in scored[:limit]:
        path = vault / "wiki" / (p.get("path") or "")
        summ = " ".join((p.get("summary") or "").split())[:100]
        print(f"  {score:5.2f}  [{p.get('tier', '?'):6}] {p.get('slug')}")
        print(f"         {summ}")
        print(f"         {path}")
    return 0
