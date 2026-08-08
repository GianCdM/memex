"""Evidence and risk gates for ChangeSet promotion."""

from __future__ import annotations

import json
from pathlib import Path

from . import canon as canon_mod

_HIGH_IMPACT_TERMS = frozenset({
    "owner", "ownership", "responsável", "prazo", "deadline", "commitment",
    "compromisso", "preference", "preferência",
    # person / team / sensitive / conflict coverage (Portuguese + English)
    "time", "equipe", "equipes", "squad", "liderança", "lideranca",
    "gestor", "gestora", "funcionário", "funcionario", "sensível",
    "sensivel", "conflito", "conflitos", "pessoa", "pessoas",
    "contratação", "contratacao", "promoção", "promocao", "salário",
    "salario", "conflict", "sensitive", "team", "hire", "salary",
})


def validate_evidence(vault: Path, change: dict) -> list[dict]:
    """Per-claim evidence anchors against the raw source.

    For `kind: doc` (ADOPT path) the proposal is a faithful near-verbatim copy of
    an already-curated document — per-claim quote-match is the wrong gate. A doc
    proposal is "supported" when its body materially preserves the raw document's
    content; that is judged by body fidelity (see `classify_risk`), so here a doc
    with claims that don't quote-match is NOT auto-rejected. For `kind: raw`
    (session distillation) the strict per-claim anchor rule applies.
    """
    kind = (change.get("source") or {}).get("kind", "raw")
    outcomes = []
    for claim in change.get("claims", []):
        anchors = claim.get("evidence") or []
        claim_outcome = "unsupported"
        for anchor in anchors:
            path = Path(vault) / str(anchor.get("raw") or "")
            quote = str(anchor.get("quote") or "")
            if not path.is_file() or not quote:
                continue
            if canon_mod.file_hash(path) != anchor.get("raw_sha256"):
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            start = max(1, int(anchor.get("start_line") or 1))
            end = min(len(lines), int(anchor.get("end_line") or start))
            excerpt = "\n".join(lines[start - 1:end])
            if quote in excerpt:
                claim_outcome = "supported"
                break
        if kind == "doc" and claim_outcome == "unsupported":
            # ADOPT: a faithful doc copy may not carry quote-exact claims; the
            # body-fidelity check in classify_risk decides. Do not auto-reject.
            claim_outcome = "doc_faithful"
        outcomes.append({"claim": claim.get("text", ""), "outcome": claim_outcome})
    return outcomes


def classify_risk(change: dict, evidence: list[dict], fidelity: dict) -> str:
    # Non-raw sources (code, tidy) have no raw transcript to verify against —
    # they are always reviewed by a human, never auto-applied. `relink` is the
    # exception: a fully deterministic, claim-free, non-destructive wikilink
    # repair (no LLM) — it may auto-apply, still subject to the section,
    # operation, and high-impact gates below.
    kind = (change.get("source") or {}).get("kind", "raw")
    if kind != "raw" and kind != "doc" and kind != "relink":
        return "review"
    if any(item.get("outcome") not in ("supported", "doc_faithful") for item in evidence):
        return "archive"
    if fidelity.get("outcome") not in ("supported", "doc_faithful") and kind != "doc":
        return "review"
    # ADOPT (doc): a faithful near-verbatim copy of a curated document may
    # auto-apply when its body preserves the raw's content — verified by the
    # independent fidelity gate (verify_fidelity returns supported for a doc
    # whose proposed body preserves the source), not by per-claim quote-match.
    if kind == "doc":
        # A `partial` doc is allowed to proceed — "preserves all durable content
        # but light reformat/adds a link" is a legitimate, reversible adoption.
        # Invented material is caught as `unsupported`/`conflicting` by the
        # outcome gate above, so partial does not mean hallucination here.
        if fidelity.get("outcome") not in ("supported", "doc_faithful", "partial"):
            return "review"
    # Structured `value` contract: `meta` is a work-log, not page content — it
    # must NOT auto-apply. `same` blocks only for an UPDATE (body ~unchanged vs
    # the existing page = nothing new); for a CREATE there is no prior body, so
    # `same`/missing means "faithful new page" and proceeds. `new` and missing
    # proceed.
    value = fidelity.get("value")
    section = (change.get("classification") or {}).get("section")
    if value == "meta":
        return "review"
    if value == "same" and change.get("operation") == "update":
        return "review"
    # `partial` is NOT a hard block: for doc adoption, "preserves all durable
    # content but light reformat / adds a link" is a legitimate improvement and
    # reversible. Material the verifier flags as invented comes back as
    # `unsupported`/`conflicting` (which the outcome gate already blocks), not
    # `partial`. So a partial faithful doc proceeds.
    if section in {"entities", "decisions"} or change.get("operation") in {"reclassify", "merge", "archive"}:
        return "review"
    text = " ".join(str(c.get("text", "")) for c in change.get("claims", [])).lower()
    if any(term in text for term in _HIGH_IMPACT_TERMS):
        return "review"
    return "auto_apply"


FIDELITY_PROMPT = """You verify whether a proposed wiki update is faithful to its source.
Return STRICT JSON only:
{{"outcome":"supported|partial|unsupported|conflicting","reason":"short explanation"}}

SOURCE CONTENT:
{evidence}

CURRENT PAGE:
{current}

PROPOSED BODY:
{proposed}
"""

# The verifier returns a structured contract: faithfulness + value. `value`
# tells whether the proposal adds NEW durable knowledge vs the current page:
#   "new"    — adds durable content not already present (real contribution)
#   "same"   — no-op: body ~unchanged from the current page (nothing new)
#   "meta"   — the body is a work-log/meta-narrative about editing the wiki
#              ("Pronto! Criei...", "a memória já existia..."), NOT page content
DOC_FIDELITY_PROMPT = """You verify whether a proposed wiki page faithfully ADOPTS a source document.
For document adoption the rule is BODY FIDELITY, not word-for-word quoting: the
proposed body must preserve the source document's durable content (sections,
facts, tables, decisions) — light reformatting and normalizing heading levels are
expected and allowed. It must NOT invent material absent from the source, drop
significant content, or drift scope.
Return STRICT JSON only:
{{"outcome":"supported|partial|unsupported|conflicting","value":"new|same|meta","reason":"short explanation"}}

SOURCE DOCUMENT:
{evidence}

CURRENT PAGE:
{current}

PROPOSED BODY:
{proposed}
"""


def _current_page_body(vault: Path, change: dict, max_chars: int = 4000) -> str:
    """The current canonical page body a doc ChangeSet would update, so the
    verifier can judge whether the proposal adds NEW value vs restates."""
    cls = change.get("classification") or {}
    section = cls.get("section") or "topics"
    slug = cls.get("slug")
    if not slug:
        return "(no target page)"
    p = Path(vault) / "wiki" / section / f"{slug}.md"
    if not p.exists():
        return "(no existing page — this is a new page)"
    t = p.read_text(encoding="utf-8", errors="ignore")
    from .format import read_frontmatter
    _, body = read_frontmatter(t)
    return (body or t)[:max_chars]


def verify_fidelity(vault: Path, change: dict, *, kind: str, model: str, settings: dict) -> dict:
    from . import providers
    source_kind = (change.get("source") or {}).get("kind", "raw")
    current = _current_page_body(vault, change)
    if source_kind == "doc":
        prompt = DOC_FIDELITY_PROMPT.format(
            evidence=_source_doc_excerpt(vault, change),
            current=current,
            proposed=change.get("proposed_body", ""),
        )
    else:
        evidence = json.dumps(validate_evidence(vault, change), ensure_ascii=False)
        prompt = FIDELITY_PROMPT.format(evidence=evidence, current=current, proposed=change.get("proposed_body", ""))
    try:
        response = providers.complete(
            prompt,
            kind=kind,
            model=model,
            settings=settings,
            json_mode=True,
        )
        parsed = json.loads(response)
    except Exception as exc:
        return {"outcome": "ambiguous", "reason": f"verifier unavailable: {type(exc).__name__}"}
    outcome = parsed.get("outcome")
    if outcome not in {"supported", "partial", "unsupported", "conflicting"}:
        return {"outcome": "ambiguous", "reason": "invalid verifier response"}
    result = {"outcome": outcome, "reason": str(parsed.get("reason") or "")}
    # `value` is a structured contract (new|same|meta) — carry it through so
    # classify_risk can block no-op / meta-narrative auto-applies without
    # string-matching PT keywords.
    value = parsed.get("value")
    if value in ("new", "same", "meta"):
        result["value"] = value
    return result


def _source_doc_excerpt(vault: Path, change: dict, max_chars: int = 12000) -> str:
    """The raw source content a doc ChangeSet adopts, bounded for the prompt."""
    raw_rel = (change.get("source") or {}).get("raw")
    if not raw_rel:
        return "(no source raw referenced)"
    path = Path(vault) / str(raw_rel)
    if not path.is_file():
        return "(source raw missing)"
    text = path.read_text(encoding="utf-8", errors="ignore")
    # strip frontmatter for the fidelity read
    from .format import read_frontmatter
    _, body = read_frontmatter(text)
    return (body or text)[:max_chars]
