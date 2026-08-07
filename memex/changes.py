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

# NOTE: do NOT add `from . import synth` / `from . import views` here —
# function-local imports only (see module docstring for why).

_STATES = frozenset({"pending", "applying", "applied", "rejected", "stale", "rolled_back"})
_OPERATIONS = frozenset({"create", "update", "merge", "reclassify", "archive", "repair"})
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


def validate_structure(vault: Path, change: dict) -> list[str]:
    errors = []
    if change.get("operation") not in _OPERATIONS:
        errors.append("unknown operation")
    if not _semantic_identity(change.get("classification") or {}):
        errors.append("classification must have a semantic title and slug")
    raw_rel = (change.get("source") or {}).get("raw")
    raw_path = Path(vault) / str(raw_rel or "")
    if not raw_path.is_file():
        errors.append("source raw file is missing")
    elif canon_mod.file_hash(raw_path) != (change.get("source") or {}).get("raw_sha256"):
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


def _snapshot_before(change: dict, target_path: Path, vault: Path, index: dict) -> dict:
    """Capture the pre-mutation state: byte-for-byte page + the index dict."""
    rel = str(target_path.relative_to(vault))
    return {
        "id": change["id"],
        "operation": change.get("operation"),
        "applied_at": int(time.time()),
        "slug": (change.get("target") or {}).get("slug")
        or (change.get("classification") or {}).get("slug"),
        "before_files": {
            rel: base64.b64encode(target_path.read_bytes()).decode("ascii"),
        },
        "after_files": {},
        "index_before": index,
        "link_rewrites": {},  # Task 8 (merge/backlink rewrite) fills this
    }


# --------------------------------------------------------------------------- #
# Promoter
# --------------------------------------------------------------------------- #
def apply_changeset(vault: Path, change_id: str, *, approved: bool = False) -> dict:
    """Apply a saved ChangeSet under a per-vault lock, with rollback support.

    Steps (all under the lock): re-validate the proposal, re-verify the target
    page hash (repair/update), honour the verification route gate, snapshot the
    pre-mutation state, render + atomically write the page, rebuild the index
    and generated views, journal the transaction, and settle the state.
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
        cur = _move_state(vault, change, old_path, "applying")

        # Re-run the structural gate under the lock (the proposal may have
        # been written or its raw source changed since it was saved).
        errors = validate_structure(vault, change)
        if errors:
            _move_state(vault, change, cur, "rejected")
            return {"state": "rejected", "errors": errors}

        # Resolve the target page from the canonical index.
        index = canon_mod.load_index(vault)
        slug = (change.get("target") or {}).get("slug") or (change.get("classification") or {}).get("slug")
        page = next((p for p in index.get("pages", []) if p.get("slug") == slug), None)
        if page is None:
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

        # Verification + risk gate — the proposal must carry a fidelity outcome
        # and classify to an auto-appliable route before we mutate. We NEVER
        # call a provider here: a proposal without a seeded fidelity result is
        # parked as pending and the proposal generator (Task 6) is expected to
        # populate `verification`. archive/reject never auto-apply.
        from . import verify as verify_mod
        evidence = verify_mod.validate_evidence(vault, change)
        verification = change.setdefault("verification", {})
        outcome = verification.get("outcome")
        if outcome not in {"supported", "partial", "unsupported", "conflicting", "ambiguous"}:
            verification["outcome"] = "required"
            _move_state(vault, change, cur, "pending")
            return {"state": "pending", "reason": "fidelity verification required"}
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

        # Snapshot pre-mutation state, then render + write the page.
        manifest = _snapshot_before(change, target_path, vault, index)
        manifest_path = _manifest_path(vault, change["id"])
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(manifest_path, manifest)

        page_text = synth._render_page(
            title=page.get("title") or slug,
            tags=page.get("tags") or [],
            kind=page.get("kind") or "session",
            status=page.get("status") or "current",
            superseded_by=page.get("superseded_by"),
            sources=page.get("sources") or [],
            body=change.get("proposed_body") or "",
            project=page.get("project"),
        )
        _atomic_write(target_path, page_text)

        rel = str(target_path.relative_to(vault))
        manifest["after_files"][rel] = base64.b64encode(target_path.read_bytes()).decode("ascii")
        _atomic_write_json(manifest_path, manifest)

        # Rebuild the canonical index + generated views, then journal.
        canon_mod.write_index(vault, index.get("pages", []))
        views.write_views(vault, index)
        _append_jsonl(_txn_path(vault), {
            "ts": int(time.time()),
            "id": change["id"],
            "action": "apply",
            "operation": change.get("operation"),
            "slug": slug,
            "state": "applied",
            "manifest": str(manifest_path.relative_to(vault)),
        })

        _move_state(vault, change, cur, "applied")
        return {"state": "applied"}
    finally:
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

        for rel, encoded in (manifest.get("before_files") or {}).items():
            target = Path(vault) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(target, base64.b64decode(encoded))

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
