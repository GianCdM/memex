"""Lightweight secret scrubbing — runs before anything is written to raw/.

A regex baseline (not exhaustive — a starting net for the obvious shapes).
Scrubbing happens at ingest time, so secrets never reach a cloud LLM in synth.
"""

from __future__ import annotations

import re

_PATTERNS = [
    # key: value / key = value
    (re.compile(
        r'(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|client[_-]?secret)\b'
        r'(\s*[:=]\s*)["\']?([A-Za-z0-9_\-./+]{8,})["\']?'),
     r'\1\2<redacted>'),
    # well-known token shapes
    (re.compile(r'\b(sk-[A-Za-z0-9]{16,})\b'), '<redacted-token>'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,})\b'), '<redacted-gh-token>'),
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
    out = text or ""
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out
