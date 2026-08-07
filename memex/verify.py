"""Evidence and risk gates for ChangeSet promotion."""

from __future__ import annotations

import json
from pathlib import Path

from . import canon as canon_mod

_HIGH_IMPACT_TERMS = frozenset({"owner", "ownership", "responsável", "prazo", "deadline", "commitment", "compromisso", "preference", "preferência"})


def validate_evidence(vault: Path, change: dict) -> list[dict]:
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
        outcomes.append({"claim": claim.get("text", ""), "outcome": claim_outcome})
    return outcomes


def classify_risk(change: dict, evidence: list[dict], fidelity: dict) -> str:
    if any(item.get("outcome") != "supported" for item in evidence):
        return "archive"
    if fidelity.get("outcome") != "supported":
        return "review"
    section = (change.get("classification") or {}).get("section")
    if section in {"entities", "decisions"} or change.get("operation") in {"reclassify", "merge", "archive"}:
        return "review"
    text = " ".join(str(c.get("text", "")) for c in change.get("claims", [])).lower()
    if any(term in text for term in _HIGH_IMPACT_TERMS):
        return "review"
    return "auto_apply"


FIDELITY_PROMPT = """You verify whether a proposed wiki update is faithful to explicit source evidence.
Return STRICT JSON only:
{"outcome":"supported|partial|unsupported|conflicting","reason":"short explanation"}

SOURCE EVIDENCE:
{evidence}

CURRENT PAGE:
{current}

PROPOSED BODY:
{proposed}
"""


def verify_fidelity(vault: Path, change: dict, *, kind: str, model: str, settings: dict) -> dict:
    from . import providers
    evidence = json.dumps(validate_evidence(vault, change), ensure_ascii=False)
    try:
        response = providers.complete(
            FIDELITY_PROMPT.format(evidence=evidence, current="", proposed=change.get("proposed_body", "")),
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
    return {"outcome": outcome, "reason": str(parsed.get("reason") or "")}
