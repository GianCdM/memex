"""Lightweight text-format helpers shared across memex modules.

Parsing helpers that several modules need (frontmatter, etc.) live here so
heavyweight modules — notably the LLM step (`synth`) — never have to be
imported by modules that only need a string helper. Dependency direction is
`synth -> format` / `canon -> format`, never `canon -> synth`.
"""

from __future__ import annotations

from datetime import date as _date


def read_frontmatter(text):
    """Parse `key: value` frontmatter between `---` fences.

    Returns (meta, body) where meta is a dict built by parsing `key: value`
    lines between the fences and body is the text after the closing fence
    (leading newlines stripped). If there is no frontmatter, returns
    ({}, text) unchanged.
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = {}
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def append_historico(body: str, *, date_str: str = "", summary: str = "", raw_fname: str = "") -> str:
    """Append a changelog entry to the `## Histórico` section deterministically.

    Called after the page is applied (never from the merge prompt) so the merge
    model's output is shorter, more stable, and language-agnostic. Keeps at most
    10 entries in chronological order (oldest first, newest last). Creates the
    section if missing. The summary is typically the propose `distill` field or
    a fallback generated from the raw filename.
    """
    today = date_str or _date.today().isoformat()
    entry = f"- {today} — {summary} ([fonte](raw/{raw_fname}))" if raw_fname else f"- {today} — {summary}"
    body = (body or "").strip()
    # Find existing Histórico section (with or without the emoji)
    idx = body.find("## \U0001f4cb Histórico")  # 📋
    if idx == -1:
        idx = body.find("## Histórico")  # plain ASCII
    if idx == -1:
        return body + f"\n\n## \U0001f4cb Histórico\n\n{entry}\n"
    # Section exists — extract entries after the heading until next ## or EOF
    tail = body[idx:]
    _, _, rest = tail.partition("\n")
    entries = []
    for line in rest.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("- ") or stripped.startswith("* "):
            entries.append(stripped)
    entries.append(entry)
    entries = entries[-10:]  # keep newest 10
    changelog = "## \U0001f4cb Histórico\n\n" + "\n".join(entries) + "\n"
    return body[:idx] + changelog.strip() + "\n"
