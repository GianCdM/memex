"""memex capture — the SessionEnd / PreCompact hook (LLM-free, fast).

The hook payload carries `transcript_path`, so capture ingests exactly THAT
session — no scanning ~/.claude/projects like v1 did. Then it optionally
refreshes the workspace's docs (stat-gated, cheap) and spawns a DETACHED
`memex reflect` for the LLM work (synth + workspace-page), so the harness never
waits on a model.

PreCompact runs with --partial: ingest the transcript-so-far and stop — the
session is still alive, and the final SessionEnd capture will supersede this
note (same session id -> same raw filename -> newer content hash -> re-synth).
That way nothing is lost even if a session dies before ending cleanly.

Exit 0 always: a capture problem must never surface as a session error.
"""

from __future__ import annotations

import hashlib
import json
import os
from argparse import Namespace
from pathlib import Path

from . import config as config_mod
from . import hookio
from . import ingest as ingest_mod
from . import proc
from .sources import claude as claude_src


def run(args) -> int:
    try:
        return _run(args)
    except Exception:
        return 0  # never break the session


def _run(args) -> int:
    payload = hookio.read_payload()
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        return 0
    partial = bool(getattr(args, "partial", False))
    cwd = payload.get("cwd") or getattr(args, "workspace", None) or os.getcwd()

    seen = ingest_mod._ledger_load(vault)
    captured = 0
    fname = None  # set when a session transcript is ingested; used as reflect --priority

    # partial capture = the session is still ALIVE (PreCompact): compaction is
    # about to drop most of the conversation, so let recall re-earn pages that
    # were injected before the summary — clear this session's dedup state.
    session_id = payload.get("session_id")
    if partial and session_id:
        hookio.clear_state(vault, f"recall-{session_id}")

    # 1) this session's transcript — an explicit --transcript wins over payload.
    # Hook captures use an offset cursor so a long-lived Claude session produces
    # one immutable raw per NEW transcript window, rather than a full snapshot
    # on every compact. Manual no-payload runs retain the bulk-scan behaviour.
    tpath = getattr(args, "transcript", None) or payload.get("transcript_path")
    if tpath:
        sid = session_id or Path(tpath).stem
        path_hash = claude_src.transcript_fingerprint(tpath)
        state_name = f"capture-{sid}-{path_hash[:16]}"
        cursor = hookio.load_state(vault, state_name)
        result = claude_src.read_transcript_delta(tpath, cursor)
        status = result.get("status")
        if status == "incompatible":
            # A rewritten/truncated transcript cannot safely yield a tail. The
            # old full parser is the conservative compatibility route.
            sess = claude_src.read_transcript(tpath)
            if sess:
                # A successful full snapshot is a new base for subsequent tails.
                tstat = Path(tpath).stat()
                end = tstat.st_size
                with Path(tpath).open("rb") as handle:
                    handle.seek(max(0, end - 4096))
                    boundary = handle.read()
                fname = ingest_mod.ingest_session(
                    vault, sess, seen,
                    extra_meta={
                        "capture_mode": "full", "capture_fallback": result.get("reason"),
                        "transcript_path_hash": path_hash, "transcript_from": 0,
                        "transcript_to": end,
                        "transcript_boundary_sha256": hashlib.sha256(boundary).hexdigest(),
                    }, identity=f"full:{path_hash}:{end}")
                if fname:
                    captured += 1
                    print(f"captured full session -> raw/{fname} ({result.get('reason')})")
                hookio.save_state(vault, state_name, {
                    "version": 2, "source": "claude", "session_id": sid,
                    "transcript_path_hash": path_hash, "transcript_to": end,
                    "transcript_boundary_sha256": hashlib.sha256(boundary).hexdigest(),
                    "transcript_device": tstat.st_dev, "transcript_inode": getattr(tstat, "st_ino", 0),
                    "last_role": None, "first_timestamp": sess.get("date"),
                    "cwd": sess.get("cwd"), "title": sess.get("title"), "last_raw": fname or "",
                })
        elif status in ("initial", "delta"):
            sess = result.get("session")
            if sess:
                start, end = result["from_byte"], result["to_byte"]
                mode = "full" if status == "initial" else "transcript-delta"
                extra = {
                    "capture_mode": mode,
                    "transcript_path_hash": result["path_hash"],
                    "transcript_from": start,
                    "transcript_to": end,
                    "transcript_boundary_sha256": result["next_cursor"]["transcript_boundary_sha256"],
                }
                if cursor.get("last_raw"):
                    extra["previous_raw"] = cursor["last_raw"]
                fname = ingest_mod.ingest_session(
                    vault, sess, seen, extra_meta=extra,
                    identity=f"{result['path_hash']}:{start}:{end}")
                if fname:
                    captured += 1
                    print(f"captured {mode} -> raw/{fname}")
                next_cursor = dict(result["next_cursor"])
                next_cursor["last_raw"] = fname or cursor.get("last_raw") or ""
                hookio.save_state(vault, state_name, next_cursor)
        # ``unchanged`` deliberately creates no raw and no priority work.
    else:
        # no payload (manual run) — fall back to scanning this workspace
        ingest_mod.run(Namespace(
            vault=str(vault), all=True, workspace=cwd, doc=None,
            docs=None, index=None, source="auto", since=None,
            session=None))

    # 2) workspace docs refresh (cheap: stat+content gated) — full capture only
    if not partial and getattr(args, "docs", False) and cwd and Path(cwd).is_dir():
        ingest_mod.run(Namespace(
            vault=str(vault), all=False, workspace=None, doc=None,
            docs=cwd, index=None, source="auto", since=None,
            session=None, exclude=str(vault)))

    # 3) the slow thinking happens detached — the harness moves on immediately.
    # PreCompact (partial) AND SessionEnd both spawn it, so a mid-session
    # compact is synthesized just like an exit. The just-captured raw is passed
    # as --priority so the reflect synthesizes THIS session first (newest-first
    # order), then drains the historical backlog.
    if not getattr(args, "no_reflect", False):
        argv = [proc.memex_exe(), "reflect", "--vault", str(vault)]
        if cwd:
            argv += ["--cwd", str(cwd)]
        if fname:
            argv += ["--priority", fname]
        pid = proc.spawn_detached(argv)
        print(f"reflect spawned (pid {pid})" if pid else "reflect spawn failed (run `memex reflect` later)")
    return 0
