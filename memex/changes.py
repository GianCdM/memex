"""memex changes — ChangeSet store, structural gate, and reversible promoter.

A ChangeSet is a reviewable, JSON-serializable proposal to change one wiki page
(repair/update/create/merge/reclassify/archive). It lives under
`.memex/review/<state>/<id>.json` and moves through states:

    pending -> applying -> applied | stale | rejected -> rolled_back

`apply_changeset` is a small transaction. Under a per-vault lock it re-validates
the proposal, compares the on-disk page against the recorded target hash (for
repair/update), writes a JSON manifest under
`.memex/history/manifests/<id>.json` describing the before/after bytes and the
pre-apply index, mutates the page + index, and appends to
`.memex/transactions.jsonl`. `rollback_changeset` replays the manifest to
restore the page byte-for-byte and rebuild the index — the "recoverable, never
hard-lost" guarantee for manual wiki edits.

Import discipline: at module load this file imports ONLY stdlib plus `canon`
(which has no back-edge to `changes`). `synth` and `views` are imported
function-locally inside `apply_changeset`/`rollback_changeset` because Task 7
of the wiki-integrity plan routes `synth` THROUGH this module — a module-load
edge here would create a `changes -> synth -> changes` cycle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from . import canon as canon_mod
from . import format as format_mod

# NOTE: do NOT add `from . import synth` / `from . import views` here —
# function-local imports only (see module docstring for why).

_STATES = frozenset({"pending", "applying", "applied", "rejected", "stale", "rolled_back"})
_OPERATIONS = frozenset({"create", "update", "merge", "reclassify", "archive", "repair", "park"})
_TECHNICAL_IDENTITY = re.compile(r"^(?:note-[a-f0-9]{6,}|untitled|misc|draft|doc)(?:-|$)", re.I)


# --------------------------------------------------------------------------- #
# Path + atomic write helpers
# --------------------------------------------------------------------------- #
def _review_dir(vault: Path, state: str) -> Path:
    if state not in _STATES:
        raise ValueError(f"unknown ChangeSet state: {state}")
    path = Path(vault) / ".memex" / "review" / state
    path.mkdir(parents=True, exist_ok=True)
    return path


def compute_dedup_key(change: dict) -> str:
    """Stable key for dedup: the same (raw, slug, section, chunk_idx, operation)
    reprocessed should NOT create a duplicate ChangeSet.

    `_chunk_index` lives on the change only for chunked slices (None otherwise)
    so two different slices of the same giant session get distinct keys. Only
    the 16-char raw hash (not the whole blob) is hashed — cheap and enough to
    tell "the same raw again" apart."""
    src = change.get("source", {}) or {}
    tgt = change.get("target", {}) or {}
    idx = change.get("index_record", {}) or {}
    raw_sha = src.get("raw_sha256") or src.get("raw") or ""
    slug = tgt.get("slug") or ""
    section = idx.get("section") or ""
    chunk = change.get("_chunk_index")
    chunk_str = "" if chunk is None else str(chunk)
    op = change.get("operation") or ""
    blob = f"{raw_sha}|{slug}|{section}|{chunk_str}|{op}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_pending_dedup(vault) -> dict:
    """Return {dedup_key: change_id} for every pending ChangeSet on disk.

    The reflect reprocess path reads this once at run start so a raw that was
    killed after `save_changeset` (before the synthed flush) is recognized as
    already-in-review — no duplicate is created. Keys are re-derived from the
    persisted JSON (including `_chunk_index`), so runs agree across processes."""
    out = {}
    pd = _review_dir(vault, "pending")
    for p in sorted(pd.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[compute_dedup_key(d)] = d.get("id")
    return out


def _atomic_write(path: Path, text: str) -> None:
    """Write text to `path` via a sibling .tmp file + replace, so a crash can
    never leave a half-written JSON or Markdown file."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Byte-for-byte variant of `_atomic_write` (rollback restores raw bytes)."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _append_jsonl(path: Path, event: dict) -> None:
    """Append one JSONL event, creating the parent dir on first use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# ChangeSet persistence
# --------------------------------------------------------------------------- #
def new_changeset(*, operation, classification, source, target, claims, proposed_body, risk, reason):
    if operation not in _OPERATIONS:
        raise ValueError(f"unknown ChangeSet operation: {operation}")
    return {
        "id": uuid.uuid4().hex,
        "state": "pending",
        "operation": operation,
        "created_at": int(time.time()),
        "classification": dict(classification),
        "source": dict(source),
        "target": dict(target),
        "claims": list(claims),
        "proposed_body": proposed_body,
        "risk": risk,
        "reason": reason,
        "verification": {},
    }


def save_changeset(vault: Path, change: dict) -> Path:
    state = change.get("state", "pending")
    path = _review_dir(vault, state) / f"{change['id']}.json"
    _atomic_write(path, json.dumps(change, ensure_ascii=False, indent=2) + "\n")
    return path


def load_changeset(vault: Path, change_id: str):
    for state in _STATES:
        path = Path(vault) / ".memex" / "review" / state / f"{change_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), path
    raise FileNotFoundError(f"unknown ChangeSet: {change_id}")


def find_changesets_by_raw(vault: Path, raw_rel: str) -> list[dict]:
    """ChangeSets (any state) whose source references a given raw file.

    `remember`/MCP use this to report what one raw capture turned into — a raw
    can produce several ChangeSets across states (pending + applied). Returns
    [{"id": ..., "state": ...}] ordered by state."""
    out = []
    for state in _STATES:
        d = Path(vault) / ".memex" / "review" / state
        if not d.is_dir():
            continue
        for fp in sorted(d.glob("*.json")):
            try:
                change = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (change.get("source") or {}).get("raw") == raw_rel:
                out.append({"id": change.get("id"), "state": change.get("state")})
    return out


def _move_state(vault: Path, change: dict, old_path: Path, state: str) -> Path:
    """Persist `change` under `state` (in place), then drop the old JSON file.

    Returns the new path. Only ever moves forward/down one state directory —
    the source file is removed only after the destination is durably written.
    """
    change["state"] = state
    new_path = save_changeset(vault, change)
    try:
        Path(old_path).unlink()
    except OSError:
        pass
    return new_path


def transition_changeset(vault: Path, change_id: str, new_state: str, reason=None) -> dict:
    """Move a ChangeSet between state dirs, appending review_reason and updated_at.

    Loads the ChangeSet, stamps an optional human `review_reason` and an
    `updated_at` timestamp, then relocates the JSON under
    `.memex/review/<new_state>/`. `_review_dir` raises ValueError for states
    outside `_STATES` (a typo'd state is caught here, not at call time).
    `rejected` and `rolled_back` are valid destinations; `approve` deliberately
    does NOT go through this path — it calls `apply_changeset(..., approved=True)`
    so the full verification + mutation pipeline runs.
    """
    change, old_path = load_changeset(vault, change_id)
    if reason is not None:
        change["review_reason"] = reason
    change["updated_at"] = int(time.time())
    _move_state(vault, change, old_path, new_state)
    return change


# --------------------------------------------------------------------------- #
# Structural validation
# --------------------------------------------------------------------------- #
def _semantic_identity(classification: dict) -> bool:
    slug = str(classification.get("slug") or "")
    title = str(classification.get("title") or "")
    return bool(slug and title and not _TECHNICAL_IDENTITY.match(slug) and "/" not in slug and "://" not in title)


def validate_structure(vault: Path, change: dict, *, auto_review: bool = False) -> list[str]:
    errors = []
    if change.get("operation") not in _OPERATIONS:
        errors.append("unknown operation")
    # Hands-free mode (auto_review) accepts a technical-identity slug (note-*)
    # when the proposal carries a real title — the policy is "apply what's
    # anchored, discard what isn't", so a note-* page is better than dropping
    # the content. Non-auto keeps requiring a semantic slug (human reclassifies).
    cls = change.get("classification") or {}
    if not _semantic_identity(cls):
        if not (auto_review and cls.get("title") and _TECHNICAL_IDENTITY.match(str(cls.get("slug") or ""))):
            errors.append("classification must have a semantic title and slug")
    source = change.get("source") or {}
    # Only raw-anchored proposals (kind == "raw") must carry an on-disk raw
    # file whose hash matches. Code- and tidy-sourced candidates have no raw
    # provenance (they are reviewed by a human, never fidelity-gated).
    if (source.get("kind") or "raw") == "raw":
        raw_path = canon_mod.raw_rel(vault, source.get("raw"))
        if not raw_path.is_file():
            errors.append("source raw file is missing")
        elif canon_mod.file_hash(raw_path) != source.get("raw_sha256"):
            errors.append("source raw hash does not match")
    section = (change.get("classification") or {}).get("section")
    if section not in canon_mod.CANONICAL_SECTIONS:
        errors.append("classification section is not canonical")
    return errors


# --------------------------------------------------------------------------- #
# Transaction journal helpers
# --------------------------------------------------------------------------- #
def _manifest_path(vault: Path, change_id: str) -> Path:
    return Path(vault) / ".memex" / "history" / "manifests" / f"{change_id}.json"


def _txn_path(vault: Path) -> Path:
    return Path(vault) / ".memex" / "transactions.jsonl"


def _snapshot_before(change: dict, vault: Path, index: dict, paths) -> dict:
    """Capture the pre-mutation state: byte-for-byte copies of every file the
    apply will touch (the target for update/archive/merge, plus each origin and
    every incoming-link referencer for merge), plus the pre-apply index dict.

    A file that does not exist (a CREATE target) is skipped — `before_files`
    stays empty for that rel so rollback knows it must DELETE the created file
    instead of restoring bytes. Task 8 generalised this from a single target to
    an arbitrary set of vault-relative files so archive/merge can snapshot the
    whole mutation set at once."""
    before = {}
    for p in paths:
        rel = str(Path(p).relative_to(vault))
        try:
            before[rel] = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        except OSError:
            pass  # create: no pre-existing file to snapshot
    return {
        "id": change["id"],
        "operation": change.get("operation"),
        "applied_at": int(time.time()),
        "slug": (change.get("target") or {}).get("slug")
        or (change.get("classification") or {}).get("slug"),
        "before_files": before,
        "after_files": {},
        # Deep copy: archive/merge mutate `index` in place AFTER the snapshot,
        # so a reference here would corrupt the rollback restore point.
        "index_before": json.loads(json.dumps(index)),
        "link_rewrites": {},  # merge/backlink rewrite fills this
    }


# --------------------------------------------------------------------------- #
# Archive / merge primitives (Task 8)
# --------------------------------------------------------------------------- #
def _set_frontmatter(text, updates):
    """Rewrite keys in the leading YAML frontmatter block, preserving every
    other line and the body byte-for-byte. New keys are appended after existing
    ones. Returns None when the text has no `---` frontmatter block.

    Used by archive/merge to flip a history copy to `status: archived` /
    `status: superseded` (plus `archive_reason` / `superseded_by`) without
    touching the page's other fields or its body."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    lines = text[3:end].splitlines()
    body = text[end + 4:]
    for key, value in updates.items():
        needle = key + ":"
        for i, line in enumerate(lines):
            if line.startswith(needle):
                lines[i] = f"{key}: {value}"
                break
        else:
            lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def rewrite_incoming_links(vault, pages, origins, target_slug):
    """Scan canonical page bodies for plain `[[<origin>]]` wikilinks and return
    the rewritten file texts keyed by vault-relative path, plus the aliased-link
    review findings.

    Only a bare `[[origin-slug]]` is rewritten (exact regex below); aliased
    links (`[[origin|alias]]`) and arbitrary text are deliberately NOT touched
    in this increment — any aliased link found is surfaced as a review finding
    for the manifest instead.

    Returns (rewrites, findings):
      rewrites: {vault-relative page path: full rewritten file text}
      findings: {origin_slug: [{"page": rel, "link": "[[origin|alias]]"}]}
    """
    rewrites = {}
    findings = {}
    for page in pages:
        rel = page.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        fp = Path(vault) / "wiki" / rel
        if not fp.is_file():
            continue
        text = fp.read_text(encoding="utf-8")
        out = text
        for old_slug in origins:
            out = re.sub(r"\[\[" + re.escape(old_slug) + r"\]\]", f"[[{target_slug}]]", out)
            for m in re.finditer(r"\[\[" + re.escape(old_slug) + r"\|", text):
                end = text.find("]]", m.start())
                link = text[m.start():end + 2] if end != -1 else text[m.start():]
                findings.setdefault(old_slug, []).append({"page": rel, "link": link})
        if out != text:
            rewrites[rel] = out
    return rewrites, findings


def _plan_merge(vault: Path, change: dict, index: dict, target_page: dict):
    """Validate + plan a mechanical merge before any bytes are touched.

    Requires `origins` to be a non-empty list of distinct current canonical
    TOPICS slugs, each in the same section as the target. Resolves the origin
    records and pre-scans every canonical page body for plain `[[origin]]`
    links. Returns a plan dict, or None when the proposal is not mechanically
    mergeable (the caller parks it pending)."""
    raw_origins = change.get("origins") or []
    if not isinstance(raw_origins, list) or not raw_origins:
        return None
    slugs = list(dict.fromkeys(str(o) for o in raw_origins))  # distinct, ordered
    target_section = target_page.get("section") or "topics"
    origin_pages = []
    for s in slugs:
        if s == target_page.get("slug"):
            return None  # an origin can never be the merge target itself
        rec = next((p for p in index.get("pages", []) if p.get("slug") == s), None)
        if rec is None or rec.get("status") != "current":
            return None
        if (rec.get("section") or "topics") != "topics":
            return None  # origins must be topics, never decisions/entities
        if (rec.get("section") or "topics") != target_section:
            return None  # origin/target section mismatch
        origin_pages.append(rec)
    rewrites, findings = rewrite_incoming_links(vault, index.get("pages", []),
                                                slugs, target_page.get("slug"))
    return {
        "origins": origin_pages,
        "origin_slugs": slugs,
        "origin_paths": [Path(vault) / "wiki" / p["path"] for p in origin_pages],
        "rewrites": rewrites,
        "findings": findings,
    }


def _apply_archive(vault: Path, change: dict, page: dict, target_path: Path,
                   index: dict, manifest: dict) -> None:
    """Move a page into recovery history as `status: archived` and drop it from
    the visible wiki + canonical index. The history copy (with `archive_reason`)
    is the recoverable audit trail — rollback restores the visible file from the
    manifest and leaves the history copy in place."""
    reason = change.get("reason") or ""
    source = target_path.read_text(encoding="utf-8")
    updated = _set_frontmatter(source, {"status": "archived"}) or source
    if reason:
        updated = _set_frontmatter(updated, {"archive_reason": reason}) or updated
    history = canon_mod.history_path(vault, page)
    history.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(history, updated)
    try:
        target_path.unlink()
    except OSError:
        pass
    index["pages"] = [p for p in index.get("pages", []) if p.get("slug") != page.get("slug")]


def _apply_merge(vault: Path, change: dict, target_page: dict, target_path: Path,
                 index: dict, plan: dict, manifest: dict) -> None:
    """Consolidate the target body, rewrite incoming links to the target, and
    supersede each origin into recovery history (`status: superseded`,
    `superseded_by: [[<target>]]`), dropping origins from the canonical index.

    The history copies + manifest `link_rewrites`/`origins`/`superseded_by` are
    the durable audit trail; rollback restores target + origins + rewritten
    referencers from `before_files` and leaves the history copies in place."""
    # function-local: synth imports changes in Task 7 — module-load edge cycles
    from . import synth

    slug = target_page.get("slug")

    # 1. Consolidated target body.
    page_text = synth._render_page(
        title=target_page.get("title") or slug,
        tags=target_page.get("tags") or [],
        kind=target_page.get("kind") or "session",
        status=target_page.get("status") or "current",
        superseded_by=None,
        sources=target_page.get("sources") or [],
        body=change.get("proposed_body") or "",
        project=target_page.get("project"),
    )
    _atomic_write(target_path, page_text)
    manifest["after_files"][str(target_path.relative_to(vault))] = (
        base64.b64encode(target_path.read_bytes()).decode("ascii"))

    # 2. Rewrite incoming-link pages. The target/origin bodies are governed by
    #    proposed_body / supersession, so only third-party referencers are
    #    written — but every found rewrite is recorded in the manifest.
    # `plan["rewrites"]` is keyed by vault-relative page path (e.g.
    # "topics/slug.md"), so `skip` must use the SAME vault-relative style for
    # the target and every origin — mixing in full paths (or a `wiki/`-prefixed
    # relpath) would never match and the origin pages would be rewritten and
    # recorded in `after_files` even though step 3 supersedes and deletes them.
    skip = {target_page["path"]}
    skip |= {p["path"] for p in plan["origins"]}
    link_rewrites = {
        origin_slug: {"rewrites": [], "aliased_links": plan["findings"].get(origin_slug, [])}
        for origin_slug in plan["origin_slugs"]
    }
    for rel, new_text in (plan["rewrites"] or {}).items():
        for origin_slug in plan["origin_slugs"]:
            link_rewrites[origin_slug]["rewrites"].append({"page": rel})
        if rel in skip:
            continue
        fp = Path(vault) / "wiki" / rel
        _atomic_write(fp, new_text)
        manifest["after_files"][rel] = base64.b64encode(fp.read_bytes()).decode("ascii")
    manifest["link_rewrites"] = link_rewrites

    # 3. Supersede each origin into recovery history + drop from the index.
    for origin in plan["origins"]:
        opath = Path(vault) / "wiki" / origin["path"]
        source = opath.read_text(encoding="utf-8")
        updated = _set_frontmatter(source, {"status": "superseded"}) or source
        updated = _set_frontmatter(updated, {"superseded_by": f"[[{slug}]]"}) or updated
        history = canon_mod.history_path(vault, origin)
        history.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(history, updated)
        try:
            opath.unlink()
        except OSError:
            pass
    origin_slugs = {p.get("slug") for p in plan["origins"]}
    index["pages"] = [p for p in index.get("pages", []) if p.get("slug") not in origin_slugs]

    manifest["origins"] = plan["origin_slugs"]
    manifest["superseded_by"] = slug


# --------------------------------------------------------------------------- #
# Promoter
# --------------------------------------------------------------------------- #
def apply_changeset(vault: Path, change_id: str, *, approved: bool = False,
                    _lock: Path | None = None, auto_review: bool = False,
                    defer_views: bool = False) -> dict:
    """Apply a saved ChangeSet under a per-vault lock, with rollback support.

    Steps (all under the lock): re-validate the proposal, re-verify the target
    page hash (repair/update), honour the verification route gate, snapshot the
    pre-mutation state, render + atomically write the page, rebuild the index
    and generated views, journal the transaction, and settle the state.

    `_lock` is an internal escape hatch: when the CALLER already holds the
    per-vault synth lock (synth._run_impl routes its own proposals through
    apply_changeset), it passes that lock path so the promoter reuses the SAME
    lock instead of deadlocking on itself ("vault busy") and never unlinks a
    lock it did not create.

    `defer_views=True` (synth batch mode) keeps the per-apply index write (the
    crash-recovery unit) but skips regenerating the machine-owned views tree;
    the caller flushes views ONCE at the end of the run from its own in-memory
    index — turning K full view rebuilds into one.
    """
    # function-local: synth/views import changes in Task 7 — a module-load
    # edge here would cycle
    from . import synth
    from . import views

    owns_lock = _lock is None
    lock = _lock if _lock is not None else synth._acquire_lock(vault)
    if lock is None:
        return {"state": "pending", "error": "vault busy"}
    try:
        change, old_path = load_changeset(vault, change_id)
        cur = _move_state(vault, change, old_path, "applying")

        # Re-run the structural gate under the lock (the proposal may have
        # been written or its raw source changed since it was saved).
        errors = validate_structure(vault, change, auto_review=auto_review)
        if errors:
            _move_state(vault, change, cur, "rejected")
            return {"state": "rejected", "errors": errors}

        # Resolve the target page. `index_record` (stashed by the generator)
        # is authoritative — it carries the merged title/tags/kind/sources that
        # a plain index lookup would miss. CREATE changesets build the record
        # from classification when no index_record was stashed.
        index = canon_mod.load_index(vault)
        slug = (change.get("target") or {}).get("slug") or (change.get("classification") or {}).get("slug")
        rec = change.get("index_record")
        if isinstance(rec, dict):
            page = dict(rec)
            if page.get("section") not in canon_mod.CANONICAL_SECTIONS:
                page["section"] = (change.get("classification") or {}).get("section") or "topics"
            if not page.get("path"):
                page["path"] = f"{page['section']}/{slug}.md"
            existing_idx = next((p for p in index.get("pages", []) if p.get("slug") == slug), None)
            if existing_idx is None:
                index["pages"] = list(index.get("pages", [])) + [page]
            else:
                index["pages"] = [page if p.get("slug") == slug else p
                                  for p in index.get("pages", [])]
        else:
            page = next((p for p in index.get("pages", []) if p.get("slug") == slug), None)
            if page is None and change.get("operation") == "create":
                section = (change.get("classification") or {}).get("section") or "topics"
                page = {
                    "slug": slug,
                    "title": (change.get("classification") or {}).get("title") or slug,
                    "section": section,
                    "kind": "session",
                    "status": "current",
                    "tags": [],
                    "sources": [],
                    "summary": "",
                    "project": (change.get("classification") or {}).get("project"),
                    "path": f"{section}/{slug}.md",
                }
                index["pages"] = list(index.get("pages", [])) + [page]
            elif page is None:
                _move_state(vault, change, cur, "stale")
                return {"state": "stale", "error": f"target page not in canonical index: {slug}"}
        target_path = Path(vault) / "wiki" / page["path"]

        # repair/update must not clobber a page that moved under us.
        if change.get("operation") in ("repair", "update"):
            if not target_path.is_file():
                _move_state(vault, change, cur, "stale")
                return {"state": "stale", "error": "target page file is missing"}
            on_disk = target_path.read_text(encoding="utf-8")
            expected = (change.get("target") or {}).get("expected_page_sha256")
            if canon_mod.page_body_hash(on_disk) != expected:
                _move_state(vault, change, cur, "stale")
                return {"state": "stale"}
        elif change.get("operation") == "merge":
            # A merge may be filed in a dry-run and approved later — before
            # consolidating over it, re-validate the target body hash so a
            # stale proposed_body is never rendered over a target that moved.
            # The check is skipped when the ChangeSet carries no expected hash
            # (tidy/identity-audit merge candidates don't record one).
            expected = (change.get("target") or {}).get("expected_page_sha256")
            if expected:
                if not target_path.is_file():
                    _move_state(vault, change, cur, "stale")
                    return {"state": "stale", "error": "target page file is missing"}
                on_disk = target_path.read_text(encoding="utf-8")
                if canon_mod.page_body_hash(on_disk) != expected:
                    _move_state(vault, change, cur, "stale")
                    return {"state": "stale"}

        # Verification + risk gate — the proposal must carry a seeded outcome
        # and classify to an auto-appliable route before we mutate. We NEVER
        # call a provider here: a proposal without a seeded fidelity result is
        # parked as pending. `code_evidence_required` (analyze) is a valid
        # seeded outcome: code has no raw to verify against, so it routes to
        # review and only applies on explicit approval. archive/merge use their
        # own gate below (they auto-apply on `supported` verification instead
        # of routing through classify_risk, which would park them unconditionally);
        # `reject` routes are never auto-applied.
        from . import verify as verify_mod
        src_kind = (change.get("source") or {}).get("kind", "raw")
        mode = (change.get("source") or {}).get("mode")
        is_slice = mode in ("delta", "chunk")
        # A slice (delta/chunk) is BODY-JUDGED against its source window — its
        # per-claim anchors are metadata at best. Re-anchoring claims here would
        # mark ungrounded chunk claims "unsupported" and archive a faithful merge
        # (the 99.5%-parking bug). Skip it; classify_risk below must get [] too.
        evidence = [] if is_slice else verify_mod.validate_evidence(vault, change)
        verification = change.setdefault("verification", {})
        outcome = verification.get("outcome")
        if outcome not in {"supported", "partial", "unsupported", "conflicting",
                           "ambiguous", "code_evidence_required"}:
            verification["outcome"] = "required"
            _move_state(vault, change, cur, "pending")
            return {"state": "pending", "reason": "fidelity verification required"}
        operation = change.get("operation")

        # Evidence-first gate: a raw CREATE/UPDATE must carry at least one claim
        # with non-empty text. A claim may lack verbatim evidence anchors (the
        # quote may be missing or not match verbatim) — that is UNANCHORED, not
        # invalid. Only a totally claim-less proposal (zero claims or all-empty
        # text) is parked pending. validate_evidence returns [] for empty claims
        # and classify_risk's `any(...)` over empty is vacuously False, so a
        # claim-less proposal would otherwise reach auto_apply. Fail closed.
        # Non-raw sources (code/tidy) are always human-reviewed and carry no
        # fake raw anchor by design, so they stay on the explicit-approval path.
        # Slices (delta/chunk) are body-judged against their source window.
        has_claims = any(str(c.get("text") or "").strip() for c in (change.get("claims") or []))
        if (operation in ("create", "update") and src_kind == "raw"
                and not has_claims and not is_slice):
            verification["outcome"] = "required"
            verification["reason"] = "no evidence-anchored claims"
            _move_state(vault, change, cur, "pending")
            return {"state": "pending", "reason": "no evidence-anchored claims"}

        if operation in ("archive", "merge"):
            # Task 8 gate reconciliation: classify_risk returns `review` for
            # ANY archive/merge operation unconditionally, which would park
            # every one of them pending. The plan resolves this: archive of
            # demonstrably unsupported content and MECHANICAL duplicate merge
            # are auto-applicable when the seeded verification outcome is
            # `supported`; semantic merges (tidy manual_review) and other
            # non-verified operations still require explicit approval. So these
            # two operations do NOT route through classify_risk like
            # create/update/repair do — only their own gate applies.
            if not approved and verification.get("outcome") != "supported":
                _move_state(vault, change, cur, "pending")
                return {"state": "pending",
                        "reason": f"{operation} requires approval or supported verification"}
            verification["route"] = "auto_apply"
        else:
            if auto_review:
                # Auto-review ON: the synth's classify_risk already decided.
                # Trust the recorded route — `auto_apply` proceeds without human
                # approval; `reject`/`archive` were discarded upstream; anything
                # else still parks (defensive).
                route = verification.get("route", "review")
                if route == "auto_apply":
                    verification["route"] = "auto_apply"
                elif route in ("reject", "archive"):
                    _move_state(vault, change, cur, "rejected")
                    return {"state": "rejected", "reason": verification.get("reason", "auto-rejected")}
                else:
                    _move_state(vault, change, cur, "pending")
                    return {"state": "pending", "reason": f"{route} routes are not auto-applied"}
            else:
                route = verify_mod.classify_risk(change, evidence, verification)
                if route == "auto_apply":
                    verification["route"] = "auto_apply"
                elif route == "review":
                    if not approved:
                        _move_state(vault, change, cur, "pending")
                        return {"state": "pending", "reason": "explicit approval required"}
                    verification["route"] = "review"
                else:  # archive / reject
                    _move_state(vault, change, cur, "pending")
                    return {"state": "pending", "reason": f"{route} routes are not auto-applied"}

        # Operation-specific pre-mutation planning — validate and resolve
        # EVERYTHING (origins, section-match, link rewrites) before any bytes
        # are touched, so a bad proposal parks pending without side effects.
        merge_plan = None
        if operation == "archive":
            if page.get("section") == "decisions":
                _move_state(vault, change, cur, "pending")
                return {"state": "pending",
                        "reason": "decision pages must be superseded, not archived"}
        elif operation == "merge":
            merge_plan = _plan_merge(vault, change, index, page)
            if merge_plan is None:
                _move_state(vault, change, cur, "pending")
                return {"state": "pending",
                        "reason": "merge requires distinct current topic origins matching the target section"}

        # Snapshot the pre-mutation state of every file this apply will touch
        # (the target; for merge also each origin and every rewritten
        # referencer), then render + write.
        touch = [target_path]
        if merge_plan is not None:
            touch.extend(merge_plan["origin_paths"])
            touch.extend(Path(vault) / "wiki" / rel for rel in merge_plan["rewrites"])
        manifest = _snapshot_before(change, vault, index, touch)
        manifest_path = _manifest_path(vault, change["id"])
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(manifest_path, manifest)

        if operation == "archive":
            _apply_archive(vault, change, page, target_path, index, manifest)
        elif operation == "merge":
            _apply_merge(vault, change, page, target_path, index, merge_plan, manifest)
        else:
            body = change.get("proposed_body") or ""
            # Append the deterministic changelog entry (the merge model no
            # longer writes the changelog). Summary comes from the page record's
            # `summary` (set from propose's `distill` field); raw_fname from
            # the source's `raw` path. Both can be empty — the helper handles it.
            raw_ref = (change.get("source") or {}).get("raw") or ""
            raw_fname = Path(raw_ref).name if raw_ref else ""
            summary = (page or {}).get("summary") or ""
            body = format_mod.append_historico(body, summary=summary, raw_fname=raw_fname)
            page_text = synth._render_page(
                title=page.get("title") or slug,
                tags=page.get("tags") or [],
                kind=page.get("kind") or "session",
                status=page.get("status") or "current",
                superseded_by=page.get("superseded_by"),
                sources=page.get("sources") or [],
                body=body,
                project=page.get("project"),
            )
            _atomic_write(target_path, page_text)
            rel = str(target_path.relative_to(vault))
            manifest["after_files"][rel] = base64.b64encode(target_path.read_bytes()).decode("ascii")
        _atomic_write_json(manifest_path, manifest)

        # Rebuild the canonical index + generated views, then journal. The
        # index write is kept per-apply (it is the crash-recovery unit); views
        # are machine-owned and regenerated on demand, so a batched synth run
        # defers them to one final flush.
        canon_mod.write_index(vault, index.get("pages", []))
        if not defer_views:
            views.write_views(vault, index)
        action = operation if operation in ("archive", "merge") else "apply"
        _append_jsonl(_txn_path(vault), {
            "ts": int(time.time()),
            "id": change["id"],
            "action": action,
            "operation": change.get("operation"),
            "slug": slug,
            "state": "applied",
            "manifest": str(manifest_path.relative_to(vault)),
        })

        _move_state(vault, change, cur, "applied")
        return {"state": "applied"}
    finally:
        if owns_lock:
            try:
                lock.unlink()
            except OSError:
                pass


def rollback_changeset(vault: Path, change_id: str) -> dict:
    """Reverse an applied ChangeSet from its transaction manifest.

    Restores every recorded `before_files` entry byte-for-byte, restores the
    pre-apply index, rebuilds generated views, journals the rollback, and
    settles the ChangeSet into `rolled_back`.
    """
    # function-local: synth/views import changes in Task 7 — a module-load
    # edge here would cycle
    from . import synth
    from . import views

    lock = synth._acquire_lock(vault)
    if lock is None:
        return {"state": "pending", "error": "vault busy"}
    try:
        change, old_path = load_changeset(vault, change_id)
        manifest = json.loads(_manifest_path(vault, change_id).read_text(encoding="utf-8"))

        # Restore every pre-apply file byte-for-byte...
        for rel, encoded in (manifest.get("before_files") or {}).items():
            target = Path(vault) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(target, base64.b64decode(encoded))
        # ...and remove files this apply CREATED (a rolled-back create must not
        # leave its page behind).
        before = manifest.get("before_files") or {}
        for rel in (manifest.get("after_files") or {}):
            if rel not in before:
                try:
                    (Path(vault) / rel).unlink()
                except OSError:
                    pass

        index_before = manifest.get("index_before") or {}
        canon_mod.write_index(vault, index_before.get("pages", []))
        views.write_views(vault, index_before)

        _append_jsonl(_txn_path(vault), {
            "ts": int(time.time()),
            "id": change_id,
            "action": "rollback",
            "operation": change.get("operation"),
            "slug": manifest.get("slug"),
            "state": "rolled_back",
            "manifest": str(_manifest_path(vault, change_id).relative_to(vault)),
        })

        _move_state(vault, change, old_path, "rolled_back")
        return {"state": "rolled_back"}
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
