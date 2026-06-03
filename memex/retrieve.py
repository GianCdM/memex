"""memex retrieve — the UserPromptSubmit auto-recall hook.

Claude Code (and Codex) pass the user's prompt as JSON on stdin. This reads it,
matches it against the vault's wiki index by lexical overlap (Jaccard over
title + tags + summary + slug), and prints the top pages as additional context —
the wiki flowing back into your session, without the model deciding to search
and without you asking.

LLM-free and non-blocking: on a terse prompt, low signal, or ANY error it prints
nothing and exits 0, so it can never block or slow a prompt down (prism's rule).
Stdlib only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MIN_PROMPT_CHARS = 15   # skip terse prompts ("ok", "vai", "continua")
MAX_RESULTS = 5         # token budget per turn
MIN_SCORE = 0.05        # relevance gate
MIN_OVERLAP = 2         # require >=2 shared terms (cuts single-common-word noise)

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
    return {
        t for t in re.split(r"[\s\-_/.,;:!?()\"'\[\]{}@#]+", (text or "").lower())
        if len(t) >= 4 and t not in _STOP
    }


def _read_prompt(query_arg):
    """Prefer an explicit --query (testing); otherwise read the hook JSON on stdin."""
    if query_arg:
        return query_arg.strip()
    if sys.stdin.isatty():
        return ""
    raw = sys.stdin.read()
    if not raw.strip():
        return ""
    try:
        return (json.loads(raw).get("prompt") or "").strip()
    except (json.JSONDecodeError, ValueError):
        return raw.strip()  # raw-text fallback (non-JSON callers)


def run(args) -> int:
    try:
        prompt = _read_prompt(getattr(args, "query", None))
        if len(prompt) < MIN_PROMPT_CHARS:
            return 0

        vault = Path(args.vault).expanduser().resolve()
        try:
            index = json.loads((vault / ".memex" / "index.json").read_text())
        except (OSError, json.JSONDecodeError):
            return 0

        qtok = _tokenize(prompt)
        if len(qtok) < 2:
            return 0

        scored = []
        for p in index.get("pages", []):
            ptok = _tokenize(
                (p.get("title") or "") + " "
                + (p.get("slug") or "").replace("-", " ") + " "
                + " ".join(p.get("tags") or []) + " "
                + (p.get("summary") or "")
            )
            if not ptok:
                continue
            overlap = qtok & ptok
            if len(overlap) < MIN_OVERLAP:
                continue
            score = len(overlap) / len(qtok | ptok)
            if score >= MIN_SCORE:
                scored.append((score, p))

        if not scored:
            return 0
        scored.sort(key=lambda x: -x[0])
        top = scored[:MAX_RESULTS]

        out = [
            "<memex-wiki>",
            "Relevant pages from your second brain (memex) for this message — "
            "open the page in the vault for full detail:",
        ]
        for _, p in top:
            summ = " ".join((p.get("summary") or "").split())[:220]
            out.append(f"- [{p.get('slug')}] {p.get('title')}: {summ}")
        out.append("</memex-wiki>")
        print("\n".join(out))
        return 0
    except Exception:
        return 0  # never block the prompt
