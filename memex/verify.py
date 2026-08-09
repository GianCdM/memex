"""Evidence and risk gates for ChangeSet promotion."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import canon as canon_mod
from . import contracts as ctr


def _extract_json(s):
    """Thin alias for `contracts.parse_json` (kept for back-compat callers)."""
    return ctr.parse_json(s)


# High-impact terms route a change to review (auto_review OFF) or the strong
# judge (auto_review ON). Matched as WHOLE TOKENS, not substrings — "time" must
# not match "timeout"/"sometimes". Common Portuguese/English plurals are listed
# explicitly alongside the singular (the source is often PT and plural forms
# like "prazos"/"gestores"/"teams" must not slip past to the flash judge).
_HIGH_IMPACT_TERMS = frozenset({
    "owner", "ownership", "owners", "responsável", "responsavel",
    "responsáveis", "responsaveis", "prazo", "prazos", "deadline", "deadlines",
    "commitment", "commitments", "compromisso", "compromissos", "preference",
    "preferences", "preferência", "preferencias", "preferencia",
    # person / team / sensitive / conflict coverage (Portuguese + English)
    "time", "times", "equipe", "equipes", "squad", "squads", "liderança",
    "lideranca", "lideranças", "liderancas", "gestor", "gestora", "gestores",
    "funcionário", "funcionario", "funcionários", "funcionarios", "sensível",
    "sensivel", "sensíveis", "sensiveis", "conflito", "conflitos", "pessoa",
    "pessoas", "contratação", "contratacao", "contratações", "contratacoes",
    "promoção", "promocao", "promoções", "promocoes", "salário", "salario",
    "salários", "salarios", "conflict", "conflicts", "sensitive", "team",
    "teams", "hire", "salary", "salaries",
})
_HIGH_IMPACT_RE = re.compile(
    r"(?<![a-zà-ÿ])(" + "|".join(re.escape(t) for t in sorted(_HIGH_IMPACT_TERMS))
    + r")(?![a-zà-ÿ])",
    re.IGNORECASE,
)


def _has_high_impact(text: str) -> bool:
    """Word-boundary match of the high-impact terms (avoids "time"→"timeout")."""
    return bool(_HIGH_IMPACT_RE.search(text or ""))


def evidence_blocks(evidence: list[dict]) -> bool:
    """True when any claim's evidence is not faithful (unsupported/conflicting).
    This is DETERMINISTIC — a hallucinated/unanchored claim — and can short-
    circuit the route before spending an LLM verify call."""
    return any(item.get("outcome") not in ctr.FAITHFUL_OUTCOMES for item in evidence)


def needs_strong_verify(change: dict, strong_body_chars: int = 8000) -> bool:
    """Whether a ChangeSet must go through the strong judge (verify_model).

    Cheap (flash) judging suffices for a plain low-risk topic update; entities,
    decisions, verifier-only routing ops, high-impact-claim changes, and large
    proposed bodies keep the strong judge so the final verdict on material
    content stays trustworthy. Everything else lets the cheap judge decide."""
    cls = change.get("classification") or {}
    section = cls.get("section")
    op = change.get("operation")
    if section in {ctr.Section.ENTITIES, ctr.Section.DECISIONS}:
        return True
    if op in ctr.ROUTING_OPS:
        return True
    # Materiality is judged over claims AND the proposed body — a delta-merge
    # doc (which carries no claims) or a topics page whose BODY mentions a
    # salary/deadline/team decision must not slip past to the cheap judge.
    text = " ".join(str(c.get("text", "")) for c in change.get("claims", []))
    body = str(change.get("proposed_body") or "")
    if _has_high_impact(text) or _has_high_impact(body):
        return True
    if len(body) > strong_body_chars:
        return True
    return False


def validate_evidence(vault: Path, change: dict) -> list[dict]:
    """Per-claim evidence anchors against the raw source.

    For `kind: doc` (ADOPT path) the proposal is a faithful near-verbatim copy of
    an already-curated document — per-claim quote-match is the wrong gate. A doc
    proposal is "supported" when its body materially preserves the raw document's
    content; that is judged by body fidelity (see `classify_risk`), so here a doc
    with claims that don't quote-match is NOT auto-rejected. For `kind: raw`
    (session distillation) the strict per-claim anchor rule applies.
    """
    kind = ctr.coerce_source_kind((change.get("source") or {}).get("kind"))
    outcomes = []
    for claim in change.get("claims", []):
        anchors = claim.get("evidence") or []
        claim_outcome = ctr.Outcome.UNSUPPORTED
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
                claim_outcome = ctr.Outcome.SUPPORTED
                break
        if kind == ctr.SourceKind.DOC and claim_outcome == ctr.Outcome.UNSUPPORTED:
            # ADOPT: a faithful doc copy may not carry quote-exact claims; the
            # body-fidelity check in classify_risk decides. Do not auto-reject.
            claim_outcome = ctr.Outcome.DOC_FAITHFUL
        outcomes.append({"claim": claim.get("text", ""), "outcome": claim_outcome})
    return outcomes


def classify_risk(change: dict, evidence: list[dict], fidelity: dict, auto_review: bool = False) -> str:
    """Decide whether a ChangeSet auto-applies, needs review, or is rejected.

    `auto_review=True` (auto-review ON): the LLM verifier decides EVERYTHING —
    cases that would otherwise require a human (new entities/decisions, high
    impact terms, ambiguous evidence) are instead auto-applied or auto-rejected
    based on the verifier's fidelity verdict. Only deterministic hard-blocks
    (meta work-logs, hallucinated content) remain. This is the "hands-free"
    mode — no human approves anything.

    `auto_review=False` (default): the current behavior — high-value/ambiguous
    changes route to a human review queue.
    """
    kind = ctr.coerce_source_kind((change.get("source") or {}).get("kind"))
    if kind not in (ctr.SourceKind.RAW, ctr.SourceKind.DOC, ctr.SourceKind.RELINK):
        return ctr.Route.REVIEW
    # A verified delta merge is judged by BODY FIDELITY against its appended
    # tail (propose was skipped, so there are no per-claim anchors). Treat it
    # like a doc: `partial` is an acceptable bounded incorporation (the tail is
    # distilled, not transcribed), and an uncertain verdict is parked, never
    # discarded.
    is_delta = (change.get("source") or {}).get("mode") == "delta"
    # Unsupported/conflicting claim evidence. In NON-auto-review this parks for
    # review (archive). In auto-review, an UNSUPPORTED claim (its quote didn't
    # anchor verbatim) can be a paraphrase rather than a hallucination — park it
    # (never discard the raw on an anchor miss). Only an explicitly CONFLICTING
    # claim (contradicts the source) is a hard reject/discard.
    if any(item.get("outcome") not in ctr.FAITHFUL_OUTCOMES for item in evidence):
        if not auto_review:
            return ctr.Route.ARCHIVE
        if any(item.get("outcome") == ctr.Outcome.CONFLICTING for item in evidence):
            return ctr.Route.REJECT
        return ctr.Route.REVIEW
    # For a non-doc (session), an un-faithful fidelity verdict routes to reject
    # in auto-review — EXCEPT `ambiguous`, which means the verifier could not
    # judge (not a content verdict). An ambiguous session note must be PARKED
    # (saved pending) in auto-review, never discarded: destroying a raw because
    # the judge was uncertain is data loss, not quality.
    if fidelity.get("outcome") not in ctr.FAITHFUL_OUTCOMES and kind != ctr.SourceKind.DOC and not is_delta:
        if not auto_review:
            return ctr.Route.REVIEW
        if fidelity.get("outcome") == ctr.Outcome.AMBIGUOUS:
            return ctr.Route.REVIEW
        return ctr.Route.REJECT
    # ADOPT (doc): faithful near-verbatim adoption may proceed. `partial` is
    # allowed (preserves all durable content, light reformat) — invented
    # material comes back as unsupported/conflicting, not partial. An ambiguous
    # doc keeps its contract (ambiguous-with-error is caught by the synth retry
    # path BEFORE classify_risk).
    if kind == ctr.SourceKind.DOC:
        if fidelity.get("outcome") not in ctr.FAITHFUL_OUTCOMES | {ctr.Outcome.PARTIAL}:
            return ctr.Route.REJECT if auto_review else ctr.Route.REVIEW
    elif is_delta:
        # A session-delta is verified by BODY FIDELITY against its tail (a
        # DISTILLATION, not a transcription). Only `supported` auto-applies:
        # `partial` means DURABLE tail content was NOT reflected — parking it
        # (review) stops the checkpoint advancing past unreflected content;
        # `ambiguous` parks (an uncertain judge must never burn content).
        # unsupported/conflicting = the merge invented/contradicted → reject.
        if fidelity.get("outcome") in (ctr.Outcome.PARTIAL, ctr.Outcome.AMBIGUOUS):
            return ctr.Route.REVIEW
        if fidelity.get("outcome") not in ctr.FAITHFUL_OUTCOMES:
            return ctr.Route.REJECT if auto_review else ctr.Route.REVIEW
    # Structured `value` contract.
    value = fidelity.get("value")
    if value == ctr.Value.META:
        # A work-log, not page content — never publish (both modes).
        return ctr.Route.REJECT if auto_review else ctr.Route.REVIEW
    if value == ctr.Value.SAME:
        # True no-op — adds nothing (any operation, not just update: a CREATE
        # the verifier deems "same" has no content worth creating).
        return ctr.Route.REJECT if auto_review else ctr.Route.REVIEW
    # High-value/ambiguous routing: these need a HUMAN when auto_review is OFF,
    # but auto-review ON lets the verifier's fidelity verdict decide instead.
    section = (change.get("classification") or {}).get("section")
    is_high_value = (
        section in {ctr.Section.ENTITIES, ctr.Section.DECISIONS}
        or change.get("operation") in ctr.ROUTING_OPS
    )
    text = " ".join(str(c.get("text", "")) for c in change.get("claims", []))
    has_high_impact = _has_high_impact(text)
    if is_high_value or has_high_impact:
        if auto_review:
            # The verifier already judged fidelity. In auto-review mode we trust
            # it: faithful → apply; ambiguous/partial → apply (reversible);
            # there is no "review" bucket anymore.
            return ctr.Route.AUTO_APPLY
        return ctr.Route.REVIEW
    return ctr.Route.AUTO_APPLY


FIDELITY_PROMPT = """You verify whether a proposed wiki update is faithful to its source.
Return STRICT JSON only:
{{"outcome":"supported|partial|unsupported|conflicting","value":"new|same|meta","reason":"short explanation"}}

"outcome" judges faithfulness to SOURCE CONTENT:
- supported   — the proposal adds only content the source supports
- partial     — faithful but some source content is not reflected
- unsupported — the proposal adds durable material NOT in the source (invention)
- conflicting — the proposal contradicts the source
"value" judges what the proposal ADDS vs the CURRENT PAGE:
- "new"  — adds durable knowledge not already present
- "same" — no-op: the body is ~unchanged from the current page
- "meta" — the body is a work-log / meta-narrative about editing the wiki, not page content

SOURCE CONTENT:
{evidence}

CURRENT PAGE:
{current}

PROPOSED BODY:
{proposed}
"""

# A session-delta is verified by BODY FIDELITY against its appended tail, but
# with a DIFFERENT lens than a full session: the proposal is the CURRENT PAGE
# plus whatever the merge distilled from the tail, so the verifier must judge
# ONLY the additions (content already in the current page is out of scope) and
# must understand that the tail is DISTILLED (dropping non-durable chit-chat is
# correct, not a fidelity problem). `partial` here means DURABLE tail content
# was dropped — the caller parks it so the checkpoint never advances past
# unreflected content.
DELTA_FIDELITY_PROMPT = """You verify a DISTILLED incremental update to a wiki page.

SOURCE is the NEW tail of an AI session — the text appended since the page's
last update. PROPOSED BODY is the CURRENT PAGE plus whatever durable knowledge
the merge distilled from that tail. The tail is DISTILLED, not transcribed:
dropping chit-chat, tool noise, and dead-ends is CORRECT, not a fidelity problem.

Judge ONLY the material the proposal ADDS beyond the CURRENT PAGE:
- supported   — the added material is faithful to the tail's durable content
- partial     — the added material OMITS durable content the tail contains
                (a real decision/fact/commitment was dropped)
- unsupported — the added material invents durable content NOT in the tail
- conflicting — the added material contradicts the tail
`value` judges the proposal vs the CURRENT PAGE:
- "new"  — adds durable knowledge not already present
- "same" — adds nothing durable (no-op)
- "meta" — the added material is a work-log about editing the wiki, not content
Content already present in the CURRENT PAGE is NOT in scope — ignore it.

Return STRICT JSON only:
{{"outcome":"supported|partial|unsupported|conflicting","value":"new|same|meta","reason":"short explanation"}}

SOURCE TAIL:
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
#
# IMPORTANT — what is NOT "invented material": the merge step is explicitly
# told to add [[wikilinks]] to related pages and may add a short navigational
# section ("Relacionado" / "Ver também" / "Veja também"). Wiki-links and such
# navigation are the wiki's structure, added BY DESIGN — they are not durable
# content fabricated from nothing, so they must NOT trigger unsupported/
# conflicting. Judge invented content as DURABLE material absent from the
# source (new sections of facts, added numbers/options/profiles, altered
# thresholds), not as wiki plumbing.
DOC_FIDELITY_PROMPT = """You verify whether a proposed wiki page faithfully ADOPTS a source document.
For document adoption the rule is BODY FIDELITY, not word-for-word quoting: the
proposed body must preserve the source document's durable content (sections,
facts, tables, decisions) — light reformatting and normalizing heading levels are
expected and allowed. It must NOT invent durable material absent from the source,
drop significant content, or drift scope.

Wiki navigation is NOT invented content: [[wikilinks]], and short cross-reference
sections like "Relacionado", "Ver também", or "Veja também" that only point to
other wiki pages, are the wiki's own structure added on purpose. Ignore them when
judging fidelity. Judge invention only as DURABLE content the source does not
contain: added facts, numbers, options, profiles, thresholds, or whole sections
of substantive material.
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


def verify_fidelity(vault: Path, change: dict, *, kind: str, model: str, settings: dict,
                    source_text: str | None = None, source_chars: int = 12000) -> dict:
    """`source_text` overrides the source the verifier judges against (a delta
    merge passes the appended TAIL, so the verifier sees exactly what was added
    instead of only the first N chars of a long doc). `source_chars` bounds the
    default doc-source excerpt (a per-vault `verify_source_chars` limit)."""
    from . import providers
    source_kind = (change.get("source") or {}).get("kind", "raw")
    is_delta = (change.get("source") or {}).get("mode") == "delta"
    # A delta must be verified against the FULL current page so the verifier can
    # isolate what the merge ADDED — a 4k cap would hide the base and turn
    # carried-forward content into a false "invention". The delta source stays
    # the appended tail.
    current = _current_page_body(vault, change,
                                 max_chars=source_chars if is_delta else 4000)
    if source_kind == "doc":
        # ADOPT: body fidelity against the source document (or the delta tail).
        evidence = source_text if source_text is not None else \
            _source_doc_excerpt(vault, change, max_chars=source_chars)
        prompt = DOC_FIDELITY_PROMPT
    elif is_delta:
        # A session-delta carries no per-claim anchors (propose was skipped) —
        # judge the DISTILLED additions against the appended tail itself, not a
        # JSON list of claim quotes (which would be "[]" → spurious unsupported).
        evidence = source_text or "(delta missing)"
        prompt = DELTA_FIDELITY_PROMPT
    else:
        # Full session distillation: per-claim quote-anchors are the gate.
        evidence = json.dumps(validate_evidence(vault, change), ensure_ascii=False)
        prompt = FIDELITY_PROMPT
    prompt = prompt.format(evidence=evidence, current=current,
                           proposed=change.get("proposed_body", ""))
    try:
        response = providers.complete(
            prompt,
            kind=kind,
            model=model,
            settings=settings,
            json_mode=True,
        )
        # Robust parse — a claude -p response can carry markdown fences or
        # trailing prose around the JSON; strict json.loads would turn a good
        # verdict into a spurious retry. Only a genuinely empty/unparseable
        # output is an infra failure.
        parsed = ctr.parse_json(response)
    except Exception as exc:
        # Infra failure (model down), NOT a content verdict.
        # `error=True` tells the caller to RETRY the raw (stay pending) instead
        # of treating `ambiguous` as a rejection. In auto-review mode a rejected
        # raw is discarded forever — a transient verifier failure must never burn
        # a good note.
        return {"outcome": ctr.Outcome.AMBIGUOUS, "error": True,
                "reason": f"verifier unavailable: {type(exc).__name__}"}
    if not isinstance(parsed, dict) or not parsed.get("outcome"):
        return {"outcome": ctr.Outcome.AMBIGUOUS, "error": True,
                "reason": "invalid verifier response"}
    outcome = parsed.get("outcome")
    if outcome not in ctr.VALID_OUTCOMES:
        return {"outcome": ctr.Outcome.AMBIGUOUS, "error": True,
                "reason": "invalid verifier response"}
    result = {"outcome": outcome, "reason": str(parsed.get("reason") or "")}
    # `value` is a structured contract (new|same|meta) — carry it through so
    # classify_risk can block no-op / meta-narrative auto-applies without
    # string-matching PT keywords.
    value = parsed.get("value")
    if value in ctr.VALID_VALUES:
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
