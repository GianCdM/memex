"""memex audit — canonical wiki integrity health report + recovery lots 0–2.

`health` counts the current, on-disk canonical pages (via `canon.canonical_pages`),
breaks them down by section, and reports the review queue depth plus obvious
identity problems (missing or `note-*` slugs). `health_run` writes both a JSON
and a Markdown snapshot under `.memex/audit/` and prints a one-line summary.

The recovery lots (`memex audit [--dry-run] [--lot N]`) scan the wiki for three
integrity problems and turn each into reversible, reviewable ChangeSets under
`.memex/review/pending/`:

  lot 0 — legacy generated artifacts (`wiki/_sugestoes.md`, root `index.md`,
          `wiki/projects/_index.md`, `wiki/projects/*.md`). These are
          machine-generated views from pre-Task-3 versions — NOT canonical pages —
          so the non-dry-run migration is a dedicated journaled move (bytes ->
          deterministic `.memex/` destination, legacy path unlinked, one
          `migrate-artifact` event in transactions.jsonl), never a ChangeSet.
  lot 1 — technical identities (`note-*`, `untitled`, `misc`, `draft`, `doc`
          slugs, title==slug, path-separator / URL-marker / prompt-template
          fragments) become pending `reclassify` ChangeSets with risk "review".
          Never auto-applied: identity repair must not guess a replacement title.
  lot 2 — mechanical duplicates (same topics section, same normalized title,
          intersecting sources, equal body hash OR equal summary, one slug
          strictly shorter) become pending `merge` ChangeSets. Non-dry-run
          applies them via `changes.apply_changeset` (byte-identical bodies make
          this a no-op content merge: proposed_body = the existing body).

`--dry-run` still GENERATES the candidate ChangeSets (so an operator can review
them) but never applies anything and never touches `wiki/`. Every run writes
both a JSON and a Markdown snapshot under `.memex/audit/latest.{md,json}`.
"""

# Notes — wiki-integrity behaviors worth documenting (Task 9 review).
#   (a) Lot-0 migration is journaled but NOT promoter-rollbackable. Each move
#       appends one `migrate-artifact` event to `.memex/transactions.jsonl`
#       carrying the base64 bytes; there is no ChangeSet manifest to roll back,
#       so recovery is a manual base64 extraction from that event.
#   (b) A lot-1 `reclassify` ChangeSet for a `note-*` (technical-identity)
#       page fails `changes.validate_structure`'s semantic gate, so
#       `memex review approve` moves it to `rejected`. It is a human signal,
#       not an appliable proposal — identity repair must never guess a
#       replacement title.
#   (c) A duplicate group with >2 pages files one `merge` ChangeSet per pair
#       (each target used once), but a non-dry-run applies only the first
#       pair; the later pair settles `stale` when its target has already been
#       superseded out of the canonical index, and is never wrongly merged —
#       fail-closed.
#   (d) Summary-equal (body-different) mechanical pairs auto-merge keeping the
#       target body (`proposed_body` = the target's body); the origin's
#       distinct body is preserved in recovery history, so no content is lost.

from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path

from . import canon as canon_mod
from . import changes as changes_mod
from .format import read_frontmatter


def _review_count(vault: Path, state: str = "pending") -> int:
    return len(list((Path(vault) / ".memex" / "review" / state).glob("*.json")))


def health(vault: Path) -> dict:
    pages = canon_mod.canonical_pages(vault)
    invalid = [p for p in pages if not p.get("slug") or p["slug"].startswith("note-")]
    # Archived (archive) / superseded (merge) originals live under
    # .memex/history/wiki/ as the recoverable audit trail — not canonical, but
    # worth surfacing so the operator sees what the promotions retired.
    history_dir = Path(vault) / ".memex" / "history" / "wiki"
    history_pages = len(list(history_dir.rglob("*.md"))) if history_dir.is_dir() else 0
    report = {
        "canonical_pages": len(pages),
        "by_section": {},
        "pending_reviews": _review_count(vault),
        "stale_reviews": _review_count(vault, "stale"),
        "invalid_current_identities": len(invalid),
        "history_pages": history_pages,
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


# --------------------------------------------------------------------------- #
# Lot 0 — legacy generated artifacts (journaled layout migration)
# --------------------------------------------------------------------------- #
# The pre-Task-3 versions shipped machine-generated views as wiki files. They
# are NOT indexed canonical pages, so `changes.apply_changeset` (which resolves
# targets from the canonical index) can never move them — a dedicated, journaled
# migration below handles the non-dry-run move. Do NOT create ChangeSets here:
# this is a layout migration, not a knowledge mutation. (Deviation from the
# plan's "through the promoter transaction" wording; rationale: the legacy files
# are non-canonical machine-owned views — regenerable, and their original bytes
# are captured in the transaction event.)
_LEGACY_ARTIFACTS = (
    ("wiki/_sugestoes.md", ".memex/audit/merge-suggestions.md"),
    ("index.md", ".memex/views/brain-index.md"),
    ("wiki/projects/_index.md", ".memex/views/projects-index.md"),
)


def scan_generated_artifacts(vault) -> list[dict]:
    """Detect the known legacy generated-artifact paths.

    Returns a list of `{"path", "dest"}` for exactly the known legacy files
    (the static ones plus every non-underscore `wiki/projects/*.md`). Unknown
    underscore files are deliberately NOT inferred here — they are surfaced
    separately via `_scan_unknown_underscore` for review, never moved."""
    vault = Path(vault)
    artifacts = []
    for legacy, dest in _LEGACY_ARTIFACTS:
        if (vault / legacy).is_file():
            artifacts.append({"path": legacy, "dest": dest})
    projects_dir = vault / "wiki" / "projects"
    if projects_dir.is_dir():
        for fp in sorted(projects_dir.glob("*.md")):
            if fp.name.startswith("_"):
                continue
            artifacts.append({
                "path": f"wiki/projects/{fp.name}",
                "dest": f".memex/views/projects/{fp.name}",
            })
    return artifacts


def _scan_unknown_underscore(vault) -> list[str]:
    """List wiki `_*.md` files that are not one of the known legacy artifacts.

    These are surfaced for a human review decision; they are never moved or
    touched by the migration."""
    vault = Path(vault)
    unknown = []
    wiki_dir = vault / "wiki"
    if wiki_dir.is_dir():
        for fp in sorted(wiki_dir.glob("_*.md")):
            if fp.name != "_sugestoes.md":
                unknown.append(f"wiki/{fp.name}")
    projects_dir = vault / "wiki" / "projects"
    if projects_dir.is_dir():
        for fp in sorted(projects_dir.glob("_*.md")):
            if fp.name != "_index.md":
                unknown.append(f"wiki/projects/{fp.name}")
    return unknown


def _migrate_artifacts(vault, artifacts) -> int:
    """Journaled lot-0 migration for non-dry-run.

    For each legacy artifact: atomically write its raw bytes to the
    deterministic `.memex/` destination (sibling .tmp + replace), unlink the
    legacy path, and append one `migrate-artifact` event to transactions.jsonl
    carrying from/to paths plus the base64 bytes (so the artifact — a regenerable
    machine view — is trivially recoverable and the move is auditable). Returns
    the number of artifacts migrated. This is NOT a ChangeSet: it never touches
    the canonical index or a canonical wiki page."""
    txn = Path(vault) / ".memex" / "transactions.jsonl"
    migrated = 0
    for artifact in artifacts:
        src = Path(vault) / artifact["path"]
        dest = Path(vault) / artifact["dest"]
        if not src.is_file():
            continue
        data = src.read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        try:
            src.unlink()
        except OSError:
            pass
        changes_mod._append_jsonl(txn, {
            "ts": int(time.time()),
            "action": "migrate-artifact",
            "from": artifact["path"],
            "to": artifact["dest"],
            "content_b64": base64.b64encode(data).decode("ascii"),
        })
        migrated += 1
    return migrated


# --------------------------------------------------------------------------- #
# Lot 1 — technical identities (pending reclassify review)
# --------------------------------------------------------------------------- #
_TECH_IDENTITY_PREFIX = re.compile(r"^(?:note-[a-f0-9]{6,}|untitled|misc|draft|doc)(?:-|$)", re.I)
_PATH_SEPARATOR = re.compile(r"[/\\]")
_URL_MARKER = re.compile(r"://")
_PROMPT_FRAGMENT = re.compile(r"\{\{|\}\}|system-prompt")


def _identity_signals(page) -> list[str]:
    """Deterministic flags for a canonical page's slug/title.

    Only topics pages are audited — decisions and entities are curated by the
    user with their own lifecycle rules, and their slugs legitimately contain
    hyphens (not path separators). A "path separator" signal requires an actual
    slash or backslash in the value (from a fallback slug that embedded a
    filesystem path), never a plain hyphen.
    """
    slug = str(page.get("slug") or "")
    title = str(page.get("title") or "")
    signals = []
    if _TECH_IDENTITY_PREFIX.match(slug):
        signals.append("technical-prefix-slug")
    if slug and title and title == slug:
        signals.append("title-equals-slug")
    for field, value in (("slug", slug), ("title", title)):
        if _PATH_SEPARATOR.search(value):
            signals.append(f"path-separator-in-{field}")
        if _URL_MARKER.search(value):
            signals.append(f"url-marker-in-{field}")
        if _PROMPT_FRAGMENT.search(value):
            signals.append(f"prompt-template-in-{field}")
    return signals


def _page_body(vault, rel: str) -> str:
    """The Markdown body (frontmatter stripped) of a canonical page file."""
    try:
        text = (Path(vault) / "wiki" / rel).read_text(encoding="utf-8")
    except OSError:
        return ""
    _, body = read_frontmatter(text)
    return body


def scan_technical_identities(vault) -> list[dict]:
    """Flag canonical pages whose slug/title look like a technical identity.

    For each flagged page a `reclassify` ChangeSet (risk "review") is created as
    a pending review candidate. The reason records which deterministic signal
    matched; no replacement title is invented and the page is never moved. This
    is always candidate generation — never auto-applied."""
    vault = Path(vault)
    findings = []
    for page in canon_mod.canonical_pages(vault):
        # Only topics pages are audited for identity: decisions and entities
        # are curated with their own lifecycle (supersede/entity rules), and
        # their hyphenated slugs are legitimate, not technical fallbacks.
        if page.get("section") != "topics":
            continue
        signals = _identity_signals(page)
        if not signals:
            continue
        slug = page.get("slug")
        rel = page.get("path")
        change = changes_mod.new_changeset(
            operation="reclassify",
            classification={
                "section": page.get("section") or "topics",
                "slug": slug,
                "title": page.get("title") or slug,
                "project": page.get("project"),
            },
            source={"kind": "identity-audit"},
            target={"slug": slug},
            claims=[],
            proposed_body=_page_body(vault, rel),
            risk="review",
            reason="technical identity (" + ", ".join(signals)
                   + "): slug/title need human reclassification",
        )
        change["verification"] = {"outcome": "manual_review", "route": "review"}
        changes_mod.save_changeset(vault, change)
        findings.append({"id": change["id"], "slug": slug, "path": rel, "signals": signals})
    return findings


# --------------------------------------------------------------------------- #
# Lot 2 — mechanical duplicates (merge candidates; auto-applied when not dry-run)
# --------------------------------------------------------------------------- #
def _normalize_title(title) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def _is_mechanical_duplicate(vault, a, b, body_hash) -> bool:
    """Deterministic mechanical-duplicate predicate. All must hold:
    same section == topics; same normalized title; no decision/entity page
    involved (implied by topics); source sets intersect; body hashes equal OR
    summaries equal; one slug strictly shorter (checked by the caller)."""
    if (a.get("section") or "topics") != "topics" or (b.get("section") or "topics") != "topics":
        return False
    if _normalize_title(a.get("title")) != _normalize_title(b.get("title")):
        return False
    sources_a = {str(s) for s in (a.get("sources") or [])}
    sources_b = {str(s) for s in (b.get("sources") or [])}
    if not (sources_a & sources_b):
        return False
    same_body = bool(a.get("path")) and body_hash(a) == body_hash(b)
    same_summary = bool(a.get("summary")) and a.get("summary") == b.get("summary")
    if not (same_body or same_summary):
        return False
    if len(a.get("slug") or "") == len(b.get("slug") or ""):
        return False  # no strict shorter slug -> not mechanical
    return True


def scan_mechanical_duplicates(vault) -> list[dict]:
    """Detect mechanical duplicate pairs over canonical pages and file a pending
    `merge` ChangeSet per pair (target = shorter slug, origins = [longer slug],
    risk low, verification supported/auto_apply). A page is only ever targeted
    once. This is deterministic equality — no LLM."""
    vault = Path(vault)
    pages = canon_mod.canonical_pages(vault)
    hash_cache: dict = {}

    def body_hash(page) -> str:
        rel = page.get("path")
        if rel not in hash_cache:
            try:
                text = (Path(vault) / "wiki" / rel).read_text(encoding="utf-8")
            except OSError:
                text = ""
            hash_cache[rel] = canon_mod.page_body_hash(text)
        return hash_cache[rel]

    groups: dict = {}
    for page in pages:
        key = _normalize_title(page.get("title"))
        if not key:
            continue
        groups.setdefault(key, []).append(page)

    findings = []
    targeted = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if not _is_mechanical_duplicate(vault, a, b, body_hash):
                    continue
                if len(a["slug"]) < len(b["slug"]):
                    target, origin = a, b
                elif len(b["slug"]) < len(a["slug"]):
                    target, origin = b, a
                else:
                    continue
                if target["slug"] in targeted:
                    continue  # a duplicate pair is only ever targeted once
                targeted.add(target["slug"])
                same_body = body_hash(target) == body_hash(origin)
                match = "equal body hash" if same_body else "equal summary"
                change = changes_mod.new_changeset(
                    operation="merge",
                    classification={
                        "section": target.get("section") or "topics",
                        "slug": target.get("slug"),
                        "title": target.get("title") or target.get("slug"),
                        "project": target.get("project"),
                    },
                    source={"kind": "identity-audit",
                            "cluster": [target.get("slug"), origin.get("slug")]},
                    target={"slug": target.get("slug")},
                    claims=[],
                    # Byte-identical bodies: the promoter renders the target with
                    # this proposed_body, so a content-preserving merge is a
                    # no-op body-wise while still superseding the origin.
                    proposed_body=_page_body(vault, target.get("path")),
                    risk="low",
                    reason="mechanical duplicate: " + match
                           + ", same normalized title, topics section, "
                           + "intersecting sources, shorter slug",
                )
                change["origins"] = [origin.get("slug")]
                change["verification"] = {"outcome": "supported", "route": "auto_apply"}
                changes_mod.save_changeset(vault, change)
                findings.append({
                    "id": change["id"],
                    "target_slug": target.get("slug"),
                    "origin_slug": origin.get("slug"),
                    "path": target.get("path"),
                })
    return findings


# --------------------------------------------------------------------------- #
# Audit runner + report
# --------------------------------------------------------------------------- #
def _write_audit_report(vault, report) -> None:
    audit_dir = Path(vault) / ".memex" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    labels = {"0": "legacy artifacts", "1": "technical identities", "2": "mechanical duplicates"}
    lines = ["# Wiki audit", "",
             f"- Generated at: {report['generated_at']}",
             f"- Dry run: {report['dry_run']}"]
    for n in ("0", "1", "2"):
        lot = report["lots"][n]
        count = lot.get("generated_artifacts", lot.get("technical_identities",
                                                       lot.get("mechanical_duplicates", 0)))
        lines.append(f"- Lot {n} ({labels[n]}): {count}")
    (audit_dir / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _log_line(msg: str, quiet: bool) -> None:
    """Per-lot summary line: stdout for humans (CLI), stderr when quiet (MCP)
    so the stdio JSON-RPC stream stays protocol-only. Diagnostics remain
    visible in the server's stderr log either way."""
    print(msg, file=sys.stderr if quiet else sys.stdout)


def run_audit(vault, *, dry_run=False, lot=None, provider=None, quiet=False) -> dict:
    """Run the requested recovery lot(s) and write `.memex/audit/latest.{md,json}`.

    Returns the report dict. `--lot` filters which lot runs; without it all
    three run. `dry_run=True` (the safe default for callers like MCP) still
    GENERATES candidate ChangeSets but never applies them and never touches
    `wiki/`. When `dry_run=False`: lot 0 runs the journaled migration, lot 2
    applies the mechanical merges via `changes.apply_changeset`, lot 1 only
    files pending reclassify ChangeSets (never auto-applied). `quiet=True`
    routes the per-lot summary lines to stderr instead of stdout, so callers
    that own stdout for protocol (the MCP server) never pollute it."""
    vault = Path(vault)
    report = {
        "generated_at": int(time.time()),
        "dry_run": bool(dry_run),
        "lots": {
            "0": {"generated_artifacts": 0, "changesets": []},
            "1": {"technical_identities": 0, "changesets": []},
            "2": {"mechanical_duplicates": 0, "changesets": []},
        },
    }
    lots = [0, 1, 2] if lot is None else [lot]

    if 0 in lots:
        artifacts = scan_generated_artifacts(vault)
        unknown = _scan_unknown_underscore(vault)
        report["lots"]["0"]["generated_artifacts"] = len(artifacts)
        report["lots"]["0"]["unknown_underscore_files"] = unknown
        migrated = 0
        if artifacts and not dry_run:
            migrated = _migrate_artifacts(vault, artifacts)
            report["lots"]["0"]["migrated"] = migrated
        _log_line(f"audit lot 0: {len(artifacts)} legacy artifact(s)"
                  + (f" — migrated {migrated}" if not dry_run else " (dry-run — not moved)"), quiet)
        if unknown:
            _log_line(f"audit lot 0: {len(unknown)} unknown underscore file(s) listed for review (not moved)", quiet)

    if 1 in lots:
        findings = scan_technical_identities(vault)
        report["lots"]["1"]["technical_identities"] = len(findings)
        report["lots"]["1"]["changesets"] = [f["id"] for f in findings]
        _log_line(f"audit lot 1: {len(findings)} technical identity page(s) -> pending reclassify review", quiet)

    if 2 in lots:
        findings = scan_mechanical_duplicates(vault)
        report["lots"]["2"]["mechanical_duplicates"] = len(findings)
        report["lots"]["2"]["changesets"] = [f["id"] for f in findings]
        applied = 0
        if findings and not dry_run:
            for finding in findings:
                result = changes_mod.apply_changeset(vault, finding["id"])
                if result.get("state") == "applied":
                    applied += 1
            report["lots"]["2"]["applied"] = applied
        _log_line(f"audit lot 2: {len(findings)} mechanical duplicate(s)"
                  + (f" — applied {applied}" if not dry_run else " (dry-run — candidates filed pending)"), quiet)

    _write_audit_report(vault, report)
    return report


def run(args) -> int:
    from . import config as config_mod
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    run_audit(
        vault,
        dry_run=bool(getattr(args, "dry_run", False)),
        lot=getattr(args, "lot", None),
        provider=getattr(args, "provider", None),
        quiet=bool(getattr(args, "quiet", False)),
    )
    return 0
