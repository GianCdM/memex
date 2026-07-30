"""Lightweight secret scrubbing — runs before anything is written to raw/.

Two independent layers, both regex-based:
  1. PII (detect_pii + scrub_pii)  — emails, CPFs, CNPJs, phones → replaced
     with labeled placeholders so you can see WHAT was redacted.
  2. Secrets (scrub) — API keys, tokens, JWTs → replaced with generic
     <redacted-*> markers.

Both run at ingest time, before the note touches disk or a cloud LLM.
"""

from __future__ import annotations

import re

# ── PII patterns (redact, don't block) ────────────────────────────────────
# Each entry is (compiled_regex, replacement_template). The replacement
# includes a label so a human reviewing the raw note can tell WHAT was found.
# Conservative patterns only — false positives are worse than false negatives.

_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Email addresses (name@domain.tld)
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
     '<email>'),
    # Brazilian CPF with punctuation — 123.456.789-01
    (re.compile(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b'),
     '<cpf>'),
    # Brazilian CNPJ with punctuation — 12.345.678/0001-90
    (re.compile(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b'),
     '<cnpj>'),
    # Brazilian phone with DDD in parens — (11) 91234-5678 or (11) 1234-5678
    (re.compile(r'\(\d{2}\)\s*\d{4,5}-\d{4}\b'),
     '<phone>'),
]


def detect_pii(text: str) -> list[str]:
    """Return the TYPES of PII found (empty list = clean).
    Called by _write_raw — if non-empty, the count is logged so the user
    can review but the note is NOT blocked.
    """
    if not text:
        return []
    found = []
    for pattern, label in _PII_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


def scrub_pii(text: str) -> str:
    """Replace PII with labeled placeholders. Runs BEFORE secret scrubbing."""
    out = text or ""
    for pattern, repl in _PII_PATTERNS:
        out = pattern.sub(repl, out)
    return out


# ── Secret patterns (redact) ──────────────────────────────────────────────

_SECRET_PATTERNS = [
    # key: value / key = value / "key":"value" (JSON) — quotes around the separator
    # are tolerated so JSON-shaped secrets (the dominant shape in AI session logs,
    # e.g. an MCP config dump) are caught too.
    (re.compile(
        r'(?i)(\b(?:api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret)\b'
        r'["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-./+]{8,})'),
     r'\1<redacted>'),
    # env-var style names: FOO_API_KEY=..., DATABRICKS_TOKEN=..., "GITLAB_..._TOKEN":"..."
    (re.compile(
        r'(?i)(\b[a-z][a-z0-9_-]*[_-](?:api[_-]?key|key|token|secret|password|passwd|pwd)'
        r'["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_\-./+=]{6,})'),
     r'\1<redacted>'),
    # well-known token shapes (caught even outside key:value form — e.g. a `ps` line)
    (re.compile(r'\b(sk-ant-[A-Za-z0-9_\-]{16,})\b'), '<redacted-anthropic-key>'),
    (re.compile(r'\b(sk[-_][A-Za-z0-9]{16,})\b'), '<redacted-token>'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,})\b'), '<redacted-gh-token>'),
    (re.compile(r'\bglpat-[A-Za-z0-9_.\-]{18,}'), '<redacted-gitlab-token>'),
    (re.compile(r'\bpplx-[A-Za-z0-9]{20,}\b'), '<redacted-perplexity-key>'),
    (re.compile(r'\bAIza[A-Za-z0-9_\-]{30,}\b'), '<redacted-google-key>'),
    (re.compile(r'\b(xox[baprs]-[A-Za-z0-9-]{10,})\b'), '<redacted-slack-token>'),
    (re.compile(r'\b(AKIA[0-9A-Z]{16})\b'), '<redacted-aws-key>'),
    (re.compile(r'\b(dapi[a-f0-9]{32,})\b'), '<redacted-databricks-token>'),
    # JWT
    (re.compile(r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b'),
     '<redacted-jwt>'),
    # private keys
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
                re.DOTALL), '<redacted-private-key>'),
    # connection strings with credentials
    (re.compile(r'(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s"\'<>]+'),
     r'\1://<redacted-connection-string>'),
]


def scrub(text: str) -> str:
    """Full scrub: PII first (labeled placeholders), then secrets (generic markers)."""
    out = scrub_pii(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out
