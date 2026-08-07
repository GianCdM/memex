"""Lightweight text-format helpers shared across memex modules.

Parsing helpers that several modules need (frontmatter, etc.) live here so
heavyweight modules — notably the LLM step (`synth`) — never have to be
imported by modules that only need a string helper. Dependency direction is
`synth -> format` / `canon -> format`, never `canon -> synth`.
"""

from __future__ import annotations


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
