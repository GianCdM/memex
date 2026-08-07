"""Canonical wiki-page predicates and index helpers.

Only current pages stored under wiki/topics, wiki/entities, and wiki/decisions
are part of the recallable graph. Generated views, history, drafts, stale index
records, and non-current lifecycle entries are never canonical.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .format import read_frontmatter

CANONICAL_SECTIONS = frozenset({"topics", "entities", "decisions"})


def load_index(vault: Path) -> dict:
    try:
        data = json.loads((Path(vault) / ".memex" / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pages": []}
    return data if isinstance(data, dict) else {"pages": []}


def write_index(vault: Path, pages: list[dict]) -> None:
    path = Path(vault) / ".memex" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pages": pages}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical_path(vault: Path, page: dict) -> Path | None:
    section = page.get("section")
    rel = page.get("path")
    if section not in CANONICAL_SECTIONS or not isinstance(rel, str):
        return None
    candidate = (Path(vault) / "wiki" / rel).resolve()
    root = (Path(vault) / "wiki" / section).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def is_canonical_record(vault: Path, page: dict) -> bool:
    if page.get("status") != "current":
        return False
    path = canonical_path(vault, page)
    return path is not None and path.is_file()


def history_path(vault: Path, page: dict) -> Path | None:
    """The recovery-history copy path for a page (.memex/history/wiki/<path>).

    Archive (status: archived) and merge (status: superseded) move the original
    page file here as the durable audit trail — visible wiki/index mutations are
    reversible, but the pre-mutation page is never hard-lost. Returns None for a
    page without a canonical `path`."""
    rel = page.get("path")
    if not isinstance(rel, str) or not rel:
        return None
    return Path(vault) / ".memex" / "history" / "wiki" / rel


def canonical_pages(vault: Path, index: dict | None = None) -> list[dict]:
    data = index if index is not None else load_index(vault)
    return [page for page in data.get("pages", []) if is_canonical_record(vault, page)]


def page_body_hash(text: str) -> str:
    """Hash only canonical body content, excluding tool-owned frontmatter."""
    _, body = read_frontmatter(text)
    normalized = "\n".join(line.rstrip() for line in body.strip().splitlines()) + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
