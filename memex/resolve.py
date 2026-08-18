"""memex resolve — ingest from a doc INDEX (a jsonl of locators).

Some workspaces don't hold documents directly — they hold an INDEX: one JSON line
per file, each with a locator and a description. memex reads the entry's OWN fields
(nothing about any specific tool is hardcoded) and resolves the real content in
tiers, cheapest first:

  1. description  — always there, auth-free: the indexer's own summary. The MAP.
  2. filesystem   — if `local_readable`, extract `local_path` (a real synced file).
  3. provider-MCP — else, if allowed + the provider can use tools (claude), ask the
                    provider to read it via the tool named IN THE ENTRY
                    (e.g. get_doc_as_markdown for a Google Doc). Best-effort.

Sensitive entries (personal data) are SKIPPED by default — a brain shouldn't slurp
a contacts/PII sheet. Override deliberately with include_sensitive.

Per-line fields (all optional but `path`): path · kind · description · local_path ·
local_readable · drive_id · mcp_read_tool · web_link · fingerprint. This is a common
doc-index shape; any indexer emitting these fields plugs in.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import extract as extract_mod

# light PII heuristic — 2+ hits in the description flags an entry as sensitive
_PII_HINTS = (
    "dados pessoais", "dados de contato", "endereço", "telefone", "celular",
    "cpf", "rg ", "alergia", "aniversário", "e-mail", "preferências alimentares",
)


def read_index(path):
    """Parse a jsonl index into a list of entries (bad lines skipped)."""
    out = []
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def is_sensitive(entry):
    desc = (entry.get("description") or "").lower()
    return sum(1 for h in _PII_HINTS if h in desc) >= 2


def _local_file(entry, base=None):
    """The real on-disk file for this entry, if readable: an absolute `local_path`,
    else `<base>/<path>` when the indexer marked it `local_readable`. None otherwise.

    Two index shapes are supported: one that stores an absolute `local_path`, and one
    that stores a relative `path` against a separate content root (`base`)."""
    lp = entry.get("local_path")
    if lp:
        p = Path(lp).expanduser()
        if p.exists():
            return p
    if entry.get("local_readable") and entry.get("path") and base:
        cand = Path(base) / entry["path"]
        if cand.exists():
            return cand
    return None


def probe_base(entries, candidates):
    """Pick the content root for relative `path`s: the first candidate dir under which
    a `local_readable` entry actually resolves. Lets the index live anywhere near its
    files (e.g. tucked in a dotfolder) without the caller wiring an explicit root."""
    cands = [Path(c) for c in candidates if c]
    for c in cands:
        for e in entries:
            if e.get("local_readable") and e.get("path") and (c / e["path"]).exists():
                return c
    return cands[0] if cands else None


def _page_from_description(entry):
    """The auth-free 'map' page: the indexer's description + a link to the real doc."""
    parts = []
    if entry.get("description"):
        parts.append(entry["description"].strip())
    link = entry.get("web_link") or entry.get("local_path")
    if link:
        parts.append(f"\n[Abrir o documento]({link})")
    return "\n".join(parts).strip()


def _resolve_via_provider(entry, prov):
    """Tier 3: ask the provider (which IS an MCP-capable harness) to read the doc
    via the tool named in the entry. Best-effort: returns None on any failure."""
    from . import providers
    tool, did = entry.get("mcp_read_tool"), entry.get("drive_id")
    if not (tool and did):
        return None
    # the full MCP tool id `mcp__<server>__<tool>` — from the entry, else built from
    # the configured server. Without a server we can't scope a safe allowlist → skip
    # (we never fall back to a permission bypass).
    full = entry.get("mcp_tool") or (
        f"mcp__{prov['mcp_server']}__{tool}" if prov.get("mcp_server") else None)
    if not full:
        return None
    prompt = (
        f"Use the `{full}` tool to read the document with id `{did}`. "
        f"Output ONLY its full content as clean Markdown — no preamble, no commentary. "
        f"If the MCP server is still connecting, keep waiting and retrying until it is ready."
    )
    try:
        out = providers.complete(prompt, model=prov["model"],
                                 settings=prov.get("settings"), allowed_tools=[full])
        out = (out or "").strip()
        return out or None
    except Exception:
        return None


def resolve_entry(entry, *, base=None, allow_mcp=False, prov=None, include_sensitive=False):
    """Resolve ONE entry to (text, method). text=None => skipped, method=reason."""
    if is_sensitive(entry) and not include_sensitive:
        return None, "sensitive (personal data) — skipped"

    # tier 2 — filesystem (a real synced file), no auth, full text via extract.py
    fp = _local_file(entry, base)
    if fp:
        text, how = extract_mod.extract(fp)
        if text and text.strip():
            return text, f"file/{how}"

    # tier 3 — MCP: claude is always the backend, so every provider is tool-capable
    if allow_mcp and prov:
        text = _resolve_via_provider(entry, prov)
        if text:
            return text, f"mcp/{entry.get('mcp_read_tool')}"

    # tier 1 — the description (the map), always available
    page = _page_from_description(entry)
    if page:
        return page, "description"

    return None, "unresolved (not local, no MCP, no description)"
