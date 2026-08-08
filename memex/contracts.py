"""memex/contracts — the pipeline's structured contracts.

Centralizes the string enums the synth→verify→promote pipeline passes around
(ChangeSet routes, verifier outcomes, the `value` contract, sections,
operations). Using `str`-subclass Enums keeps every existing string comparison
working (`Route.auto_apply == "auto_apply"` is True) while making a typo a
NameError/AttributeError instead of a silent runtime mismatch — and it gives
editors/type-checkers a single source of truth for the contract values.

The LLM-facing contracts (the JSON shapes the propose/verify steps must return)
live here too, so the code that parses them and the code that routes on them
agree on the vocabulary by construction.
"""

from __future__ import annotations

import enum
import json
import re


class _StrEnum(str, enum.Enum):
    """A str-subclass enum: compares equal to its bare string value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Route(_StrEnum):
    """Where a ChangeSet lands after verification/risk routing."""
    AUTO_APPLY = "auto_apply"
    REVIEW = "review"
    REJECT = "reject"
    ARCHIVE = "archive"
    PENDING = "pending"


class Outcome(_StrEnum):
    """Verifier fidelity verdicts (the structured `outcome` contract)."""
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"
    AMBIGUOUS = "ambiguous"
    # Internal sentinel for the doc-ADOPT path: per-claim quote-match is the
    # wrong gate for a faithful document copy; body fidelity governs instead.
    DOC_FAITHFUL = "doc_faithful"


class Value(_StrEnum):
    """Verifier `value` contract — what a proposal ADDS vs the current page."""
    NEW = "new"
    SAME = "same"
    META = "meta"


class Section(_StrEnum):
    TOPICS = "topics"
    ENTITIES = "entities"
    DECISIONS = "decisions"


class Operation(_StrEnum):
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    ARCHIVE = "archive"
    RECLASSIFY = "reclassify"
    REPAIR = "repair"


class SourceKind(_StrEnum):
    RAW = "raw"
    DOC = "doc"
    RELINK = "relink"
    CODE = "code"
    IDENTITY_AUDIT = "identity-audit"
    TIDY = "tidy"
    SESSION = "session"  # frontmatter kind on raw notes (source.kind is raw/doc)


def coerce_source_kind(value) -> SourceKind | None:
    """Tolerant `SourceKind(value)`.

    ABSENT (None/empty) → RAW: a ChangeSet without a source kind is a normal
    synth change. An UNKNOWN non-empty kind → None, which routing treats as
    NON-proceedable (→ review), so a typo or a new tool-internal kind can't
    crash routing NOR silently auto-apply as if it were a raw note.
    """
    if not value:
        return SourceKind.RAW
    try:
        return SourceKind(value)
    except ValueError:
        return None


# The ordered set of outcomes the classifier treats as "faithful enough to
# proceed" (used by verify.classify_risk). Docs additionally allow `partial`.
FAITHFUL_OUTCOMES = frozenset({Outcome.SUPPORTED, Outcome.DOC_FAITHFUL})

# The ordered set of outcomes that are LEGITIMATE verifier responses (anything
# else means the model returned garbage and the call should be retried).
VALID_OUTCOMES = frozenset({Outcome.SUPPORTED, Outcome.PARTIAL,
                            Outcome.UNSUPPORTED, Outcome.CONFLICTING})

VALID_VALUES = frozenset({Value.NEW, Value.SAME, Value.META})

VALID_SECTIONS = frozenset({Section.TOPICS, Section.ENTITIES, Section.DECISIONS})

# Verifier-only routing ops (not synth creates/updates).
ROUTING_OPS = frozenset({Operation.RECLASSIFY, Operation.MERGE, Operation.ARCHIVE})


def parse_json(text: str):
    """Robust JSON extraction via a real JSON decoder.

    Unlike a hand-rolled brace counter, `JSONDecoder.raw_decode` understands
    string literals — a `}` or `{...}` inside a quoted value can't truncate the
    scan. Tolerates markdown fences and trailing prose around the payload (a
    non-json_mode provider like `claude -p` can emit either).
    """
    if not text:
        return None
    stripped = re.sub(r"```(?:json)?", "", text).strip()
    # Probe EVERY `{`/`[` position (a `{foo}` placeholder in prose must not
    # shadow the real payload after it). The earliest opener is tried first so
    # top-level arrays win over nested objects; on failure we advance to the
    # next opener instead of giving up.
    positions = [i for i, ch in enumerate(stripped) if ch in "{["]
    for start in positions:
        try:
            obj, _ = json.JSONDecoder().raw_decode(stripped[start:])
            return obj
        except (ValueError, TypeError):
            continue
    return None
