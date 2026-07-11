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

from . import config as config_mod
from . import hookio
from . import limits as limits_mod

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


def rank(pages, prompt, lim):
    """Gate like v1 (overlap + Jaccard), rank by IDF-weighted overlap.
    Returns [(score, page)] best-first."""
    qtok = _tokenize(prompt)
    if len(qtok) < 2:
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
    if len(prompt) < lim["retrieve_min_prompt_chars"]:
        return 0
    try:
        index = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    scored = rank(index.get("pages", []), prompt, lim)
    if not scored:
        return 0

    # session dedup — never inject the same page twice into one session
    session_id = payload.get("session_id") or ""
    state_key = f"recall-{session_id}" if session_id else None
    injected = set()
    if state_key:
        injected = set(hookio.load_state(vault, state_key).get("slugs") or [])
        scored = [(s, p) for (s, p) in scored if p.get("slug") not in injected]
        if not scored:
            return 0

    top = scored[: lim["retrieve_max_results"]]

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
    return 0
