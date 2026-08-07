"""memex recall — the UserPromptSubmit auto-recall hook (long-term memory).

Reads the hook payload (prompt + session_id), scores the wiki index against
the prompt, and prints the top pages as additional context. v2 improvements
over the v1 retrieve:

- ranking: candidates still pass the v1 gates (term overlap + Jaccard floor),
  but are RANKED by IDF-weighted overlap, so rare, specific terms dominate
  ("databricks" beats "config") instead of raw set arithmetic;
- session dedup: pages already injected into THIS session (keyed by the
  payload's session_id, kept in .memex/state/) are never repeated — recall
  adds context, it doesn't re-shout it every prompt;
- actionable output: every hit includes its absolute file path so the model
  can Read the full page instead of guessing from a 220-char summary.

LLM-free and non-blocking: terse prompt, low signal, or ANY error -> print
nothing, exit 0 (a recall problem must never slow a prompt down).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import canon as canon_mod
from . import config as config_mod
from . import hookio
from . import limits as limits_mod
from . import workspace as workspace_mod
from . import providers

# bilingual stopwords (pt + en) — the content is mixed
_STOP = {
    "quando", "para", "como", "onde", "fazer", "quero", "preciso", "sobre",
    "isso", "esse", "essa", "esses", "aquele", "tudo", "agora", "depois",
    "entao", "tambem", "ainda", "porque", "qual", "quais", "mais", "menos",
    "pode", "fica", "vamos", "esta", "este", "seria", "tem", "uma", "umas",
    "with", "when", "what", "that", "this", "into", "from", "your", "have",
    "should", "would", "could", "about", "which", "there", "their", "then",
    "want", "need", "make", "does", "just", "like", "here", "them", "they",
}


def _tokenize(text):
    """Significant terms, stemmed to a 5-char prefix. The brain is bilingual
    (pt/en mixed) and tech vocab is full of cognates — prefix-stemming makes
    'alertas'~'alerts', 'migração'~'migration', 'configuramos'~'config' match
    without any language machinery. Collisions are acceptable at wiki scale."""
    return {
        t[:5] for t in re.split(r"[\s\-_/.,;:!?()\"'\[\]{}@#]+", (text or "").lower())
        if len(t) >= 4 and t not in _STOP
    }


def page_tokens(p) -> set:
    return _tokenize(
        (p.get("title") or "") + " "
        + (p.get("slug") or "").replace("-", " ") + " "
        + " ".join(p.get("tags") or []) + " "
        + (p.get("summary") or "")
    )


def _load_embeddings(vault: Path):
    """Load precomputed page embeddings (slug -> normalized vector) plus the
    meta record so the caller can detect model/dim drift. Returns
    ({}, {}) when the index hasn't been built (semantic recall stays off —
    the lexical scorer still runs). Silent on I/O errors: recall must never
    block a prompt just because embeddings are stale/missing.
    """
    embed_dir = vault / ".memex" / "embeddings"
    if not embed_dir.exists():
        return {}, {}
    out = {}
    for f in embed_dir.glob("*.jsonl"):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                vec = rec.get("vec")
                if isinstance(vec, list) and vec:
                    out[rec["slug"]] = vec
        except Exception:
            continue
    meta = {}
    meta_path = embed_dir / "_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    return out, meta


def _cosine_rank(query_vec: list[float], vectors: dict[str, list[float]]):
    """Score every page by cosine similarity. Assumes both sides were
    L2-normalized at index time (embed.py does this), so a plain dot product
    equals cosine — no sqrt in the hot path.

    Returns [(score, slug)] sorted best-first."""
    if not query_vec or not vectors:
        return []
    # Normalize query once (cheap: 384-1536 floats).
    norm = math.sqrt(sum(v * v for v in query_vec)) or 1.0
    q = [v / norm for v in query_vec]
    scored = []
    for slug, vec in vectors.items():
        if len(vec) != len(q):
            continue  # dim mismatch (model changed since indexing) — skip
        s = sum(a * b for a, b in zip(q, vec))
        scored.append((s, slug))
    scored.sort(key=lambda x: -x[0])
    return scored


def _rrf_fuse(lexical: list, semantic: list, k: int = 60):
    """Reciprocal Rank Fusion: fold two ranked lists into one WITHOUT
    normalizing scores (they live in different spaces — IDF sums for lexical
    vs cosine [-1,1] for semantic). Each list contributes 1/(k+rank) per hit;
    a page appearing near the top of BOTH lists dominates. `k=60` is the
    de-facto default across search literature. Returns [(score, page)]
    best-first.

    Inputs:
      lexical  = [(score, page), ...] as `rank()` returns
      semantic = [(score, slug), ...] as `_cosine_rank()` returns
    """
    slug_to_page = {}
    fused: dict[str, float] = {}
    for r, (_, p) in enumerate(lexical):
        slug = p.get("slug")
        if not slug:
            continue
        slug_to_page[slug] = p
        fused[slug] = fused.get(slug, 0.0) + 1.0 / (k + r + 1)
    for r, (_, slug) in enumerate(semantic):
        fused[slug] = fused.get(slug, 0.0) + 1.0 / (k + r + 1)
    out = [(fused[s], slug_to_page[s]) for s in fused if s in slug_to_page]
    out.sort(key=lambda x: -x[0])
    return out


def rank(pages, prompt, lim, min_tokens=2):
    """Gate like v1 (overlap + Jaccard), rank by IDF-weighted overlap.
    Returns [(score, page)] best-first. min_tokens=2 suppresses terse prompts
    on the hook path; interactive search passes 1 (single-term queries are
    legitimate there)."""
    qtok = _tokenize(prompt)
    if len(qtok) < min_tokens:
        return []
    docs = [(p, page_tokens(p)) for p in pages]
    docs = [(p, t) for (p, t) in docs if t]
    n_docs = len(docs) or 1
    df = {}
    for _, toks in docs:
        for t in toks:
            df[t] = df.get(t, 0) + 1

    def idf(t):
        return math.log(1.0 + (n_docs - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))

    scored = []
    for p, ptok in docs:
        overlap = qtok & ptok
        if len(overlap) < lim["retrieve_min_overlap"]:
            continue
        jaccard = len(overlap) / len(qtok | ptok)
        if jaccard < lim["retrieve_min_score"]:
            continue
        scored.append((sum(idf(t) for t in overlap), p))
    scored.sort(key=lambda x: -x[0])
    return scored


def hybrid_rank(pages, prompt, lim, vault, *, min_tokens=2, log_prefix="memex recall"):
    """Score pages by lexical + semantic (when available), fuse via RRF.

    Shared between the recall hook (`_run` below) and the interactive
    `memex search` CLI — the semantic layer stays optional, silent on any
    failure (warnings to stderr), and both callers get the same behavior.

    Returns [(score, page)] best-first. Empty list = nothing above the gates.
    """
    import sys as _sys
    lexical = rank(pages, prompt, lim, min_tokens=min_tokens)

    semantic = []
    vcfg = config_mod.load_vault(vault)
    embed_model, embed_settings = config_mod.resolve_embeddings(vault_cfg=vcfg)
    if embed_model:
        page_vectors, embed_meta = _load_embeddings(vault)
        if page_vectors:
            # Guard against model / dim drift BEFORE spending an API call.
            stored_model = embed_meta.get("model")
            stored_dim = embed_meta.get("dim")
            model_mismatch = stored_model and stored_model != embed_model
            dim_mismatch = (isinstance(stored_dim, int)
                            and any(len(v) != stored_dim for v in list(page_vectors.values())[:1]))
            if model_mismatch:
                print(f"{log_prefix}: stored embeddings were built with '{stored_model}' "
                      f"but config now says '{embed_model}' — run `memex embed --force` "
                      f"to reindex. Falling back to lexical-only.",
                      file=_sys.stderr)
            elif dim_mismatch:
                print(f"{log_prefix}: embedding dimension drift detected — run "
                      "`memex embed --force`. Falling back to lexical-only.",
                      file=_sys.stderr)
            else:
                try:
                    query_settings = dict(embed_settings)
                    if embed_settings.get("input_type") is None:
                        query_settings["input_type"] = "search_query"
                    elif embed_settings.get("input_type") == "search_document":
                        query_settings["input_type"] = "search_query"
                    vecs = providers.embed([prompt], model=embed_model,
                                           settings=query_settings)
                    if vecs:
                        q_dim = len(vecs[0])
                        p_dim = len(next(iter(page_vectors.values())))
                        if q_dim != p_dim:
                            print(f"{log_prefix}: query embedding dim {q_dim} != "
                                  f"stored dim {p_dim} — run `memex embed --force`. "
                                  "Falling back to lexical-only.",
                                  file=_sys.stderr)
                        else:
                            semantic = _cosine_rank(vecs[0], page_vectors)
                except Exception as e:
                    print(f"{log_prefix}: semantic layer failed "
                          f"({type(e).__name__}: {str(e)[:120]}) — lexical-only.",
                          file=_sys.stderr)

    if semantic and lexical:
        pool = max(lim["retrieve_max_results"] * 4, 20)
        return _rrf_fuse(lexical[:pool], semantic[:pool])
    if semantic:
        by_slug = {p["slug"]: p for p in pages}
        return [(s, by_slug[slug]) for s, slug in semantic if slug in by_slug]
    return lexical


def _workspace_fresh(meta, max_age_days) -> bool:
    """True when the workspace-page's updated timestamp is within max_age_days."""
    from datetime import datetime
    try:
        updated = datetime.strptime((meta or {}).get("updated", ""), "%Y-%m-%dT%H:%M:%SZ")
        age_h = (datetime.utcnow() - updated).total_seconds() / 3600.0
        return age_h <= int(max_age_days) * 24
    except Exception:
        return False


def run(args) -> int:
    try:
        return _run(args)
    except Exception:
        return 0  # never block the prompt


def _run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    lim = limits_mod.load(vault)

    payload = hookio.read_payload()
    prompt = (getattr(args, "query", None) or payload.get("prompt") or "").strip()
    session_id = payload.get("session_id") or ""

    # ── wiki recall (long-term memory) ──────────────────────────────────
    if len(prompt) >= lim["retrieve_min_prompt_chars"]:
        try:
            index = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index = None
        if index:
            pages = canon_mod.canonical_pages(vault, index)
            scored = hybrid_rank(pages, prompt, lim, vault, log_prefix="memex recall")
            # session dedup — never inject the same page twice into one session
            state_key = f"recall-{session_id}" if session_id else None
            if state_key:
                injected = set(hookio.load_state(vault, state_key).get("slugs") or [])
                scored = [(s, p) for (s, p) in scored if p.get("slug") not in injected]
            top = scored[: lim["retrieve_max_results"]]
            if top:
                out = [
                    "<memex-wiki>",
                    "Pages from your second brain (memex) relevant to this message — "
                    "Read the path for full detail:",
                ]
                for _, p in top:
                    summ = " ".join((p.get("summary") or "").split())[:220]
                    path = vault / "wiki" / (p.get("path") or "")
                    out.append(f"- [{p.get('slug')}] {p.get('title')}: {summ}\n  -> {path}")
                out.append('(more: `memex search "<terms>"`)')
                out.append("</memex-wiki>")
                print("\n".join(out))
                if state_key:
                    injected.update(p.get("slug") for _, p in top)
                    hookio.save_state(vault, state_key, {"slugs": sorted(injected)})

    # ── workspace injection (working memory) — for concurrent sessions ────────
    # Boot already injected this at SessionStart, but another session may
    # have updated the workspace-page since (via compact or exit). Re-inject when
    # the timestamp changes so concurrent sessions stay in sync — just like
    # wiki pages do.
    cwd = payload.get("cwd")
    if cwd:
        workspace, _root, display_name = workspace_mod.workspace_key_detail(cwd)
        if workspace:
            meta, body = workspace_mod.read_workspace(vault, workspace, cwd=cwd)
            if body and _workspace_fresh(meta, lim["boot_workspace_max_age_days"]):
                workspace_state_key = f"workspace-shown-{session_id}" if session_id else None
                current_ts = meta.get("updated", "")
                last_ts = hookio.load_state(vault, workspace_state_key).get("updated") if workspace_state_key else None
                if last_ts != current_ts:
                    body = body.strip()[: lim.get("boot_max_chars", 4000)]
                    out_lines = [
                        "<memex-workspace>",
                        f"Where you left off — workspace `{display_name or workspace}` (`{workspace}`) "
                        f"(saved {current_ts}, by {meta.get('author', '?')})",
                        body,
                        f"(full page: {workspace_mod.workspace_path(vault, workspace)})",
                        "</memex-workspace>",
                    ]
                    print("\n".join(out_lines))
                    if workspace_state_key:
                        hookio.save_state(vault, workspace_state_key, {"updated": current_ts})
    return 0
