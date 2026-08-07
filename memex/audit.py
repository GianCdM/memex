"""memex audit — canonical wiki integrity health report.

`health` counts the current, on-disk canonical pages (via `canon.canonical_pages`),
breaks them down by section, and reports the review queue depth plus obvious
identity problems (missing or `note-*` slugs). `health_run` writes both a JSON
and a Markdown snapshot under `.memex/audit/` and prints a one-line summary.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import canon as canon_mod


def _review_count(vault: Path, state: str = "pending") -> int:
    return len(list((Path(vault) / ".memex" / "review" / state).glob("*.json")))


def health(vault: Path) -> dict:
    pages = canon_mod.canonical_pages(vault)
    invalid = [p for p in pages if not p.get("slug") or p["slug"].startswith("note-")]
    report = {
        "canonical_pages": len(pages),
        "by_section": {},
        "pending_reviews": _review_count(vault),
        "stale_reviews": _review_count(vault, "stale"),
        "invalid_current_identities": len(invalid),
        "dead_links": 0,
        "suggestions": 0,
    }
    for page in pages:
        section = page.get("section", "topics")
        report["by_section"][section] = report["by_section"].get(section, 0) + 1
    return report


def write_health_report(vault: Path, report: dict) -> None:
    audit_dir = Path(vault) / ".memex" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Memex health", "", f"- Canonical pages: {report['canonical_pages']}", f"- Pending review: {report['pending_reviews']}", f"- Stale review: {report['stale_reviews']}", f"- Invalid current identities: {report['invalid_current_identities']}"]
    (audit_dir / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def health_run(args) -> int:
    from . import config as config_mod
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    report = health(vault)
    write_health_report(vault, report)
    print(f"wiki: {report['canonical_pages']} current · {report['pending_reviews']} in review · {report['invalid_current_identities']} invalid identities")
    return 0
