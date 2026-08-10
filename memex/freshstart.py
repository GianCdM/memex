"""memex/freshstart.py — one-time backlog reset.

Marks all raw captures dated BEFORE --from as processed (no LLM), optionally
archives pending ChangeSets. Preserves applied/rejected. Idempotent.
"""
from __future__ import annotations
import json, hashlib, shutil
from pathlib import Path

from . import canon as canon_mod
from . import synth as synth_mod


def _raw_date_prefix(name: str) -> str:
    # filename: YYYY-MM-DD--source--...
    return name[:10] if len(name) >= 10 and name[4:5] == "-" else ""


def run(args) -> int:
    # Resolve so the synth lock path matches what synth.run uses (`_acquire_lock`
    # keys on the resolved vault) — a lock claimed here must be the SAME file a
    # concurrent synth would try to claim.
    vault = Path(args.vault).expanduser().resolve()
    from_date = args.from_date
    dry = getattr(args, "dry_run", False)
    archive = getattr(args, "archive_pending", False)

    raw_dir = canon_mod.raw_dir(vault)
    synthed_path = vault / ".memex" / "synthed.json"  # canonical synthed path (synth.py:914); no helper
    try:
        synthed = json.loads(synthed_path.read_text(encoding="utf-8"))
    except Exception:
        synthed = {}

    raws = sorted(raw_dir.glob("*.md")) if raw_dir.exists() else []
    to_mark = [f for f in raws
               if _raw_date_prefix(f.name) and _raw_date_prefix(f.name) < from_date
               and synthed.get(f.name) is None]

    # Direct path, NOT `changes_mod._review_dir` (which mkdirs): a dry-run must
    # never mutate the vault, so the pending/ dir is only created lazily by the
    # archive move below.
    pending_dir = vault / ".memex" / "review" / "pending"
    pendings = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    archive_dir = vault / ".memex" / "review" / "archived-pre-freshstart"  # one-time migration dir; no helper

    by_month = {}
    for f in to_mark:
        m = _raw_date_prefix(f.name)[:7]
        by_month[m] = by_month.get(m, 0) + 1

    print(f"fresh-start {'(dry-run) ' if dry else ''}from {from_date}")
    print(f"  raws to mark processed: {len(to_mark)}")
    for m, n in sorted(by_month.items()):
        print(f"    {m}: {n}")
    print(f"  pending ChangeSets to archive: {len(pendings)}")

    if dry:
        print("  (dry-run — nothing mutated)")
        return 0

    # ── apply path: hold the synth lock ─────────────────────────────────────
    # A SessionEnd auto-synth firing while we mutate synthed.json is a
    # last-writer-wins race on the raw-done marks, and the pending→
    # archived-pre-freshstart moves can race a run whose dedup_set still
    # references those pendings. Stand down (return 1) if a live synth holds
    # the lock; release it in `finally` like synth.run does.
    lock = synth_mod._acquire_lock(vault)
    if lock is None:
        print("synth in progress — retry fresh-start later")
        return 1
    try:
        # mark raws
        for f in to_mark:
            h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
            synthed[f.name] = h
        synth_mod._atomic_write(synthed_path, json.dumps(synthed, indent=2) + "\n")

        # archive pendings
        if archive and pendings:
            archive_dir.mkdir(parents=True, exist_ok=True)
            for p in pendings:
                shutil.move(str(p), str(archive_dir / p.name))

        print(f"  done. marked {len(to_mark)} raws, archived {len(pendings)} pendings.")
        return 0
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
