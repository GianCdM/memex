"""memex injection — token-economy telemetry for context injection.

Every hook that prints context (boot on SessionStart, recall on every
UserPromptSubmit) logs WHAT it injected and HOW MUCH, so the owner can see
the cost of memory per session instead of guessing. claude-mem measures
injection budget deliberately (~2.250 tok/session saved); memex measures
after the fact with the same intent: `memex injection` reports bytes/tokens
per hook per session, so recall/boot tuning is data-driven.

LLM-free, stdlib-only, never-blocking: a telemetry failure must not slow a
prompt down — every error path exits silently."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import hookio

_REL = Path(".memex") / "injection.jsonl"


def log(vault, *, hook: str, session_id: str, blocks: dict) -> None:
    """Append one line per injection event.

    `blocks` maps block name -> injected chars (e.g. {"brain": 812} or
    {"wiki_pages": 940, "workspace": 2100}). A token estimate (~4 chars/tok)
    is computed at report time, not stored — the ratio is a constant."""
    try:
        entry = {"ts": int(time.time()), "hook": hook, "session": session_id or "",
                 "blocks": {k: int(v) for k, v in (blocks or {}).items()}}
        path = Path(vault) / _REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # telemetry must never break the hook


def _load(vault: Path, since_ts: int = 0) -> list[dict]:
    path = Path(vault) / _REL
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("ts", 0) >= since_ts:
            out.append(rec)
    return out


def report(vault, *, hours: int = 24) -> dict:
    """Aggregated injection cost over the last `hours`:
    per hook (calls, total/avg bytes, est. tokens) + per-session totals."""
    since = int(time.time()) - hours * 3600
    rows = _load(vault, since)
    per_hook: dict = {}
    per_session: dict = {}
    for r in rows:
        total = sum((r.get("blocks") or {}).values())
        h = per_hook.setdefault(r.get("hook", "?"),
                                {"calls": 0, "bytes": 0})
        s = per_session.setdefault(r.get("session") or "?", {"bytes": 0, "calls": 0})
        h["calls"] += 1
        h["bytes"] += total
        s["bytes"] += total
        s["calls"] += 1
    for h in per_hook.values():
        h["avg_bytes"] = h["bytes"] // h["calls"] if h["calls"] else 0
        h["est_tokens"] = h["bytes"] // 4
    for s in per_session.values():
        s["est_tokens"] = s["bytes"] // 4
    return {"ok": True, "hours": hours, "events": len(rows),
            "per_hook": per_hook, "per_session": per_session}


def run(args) -> int:
    from . import config as config_mod
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1
    out = report(vault, hours=getattr(args, "hours", None) or 24)
    if not out["events"]:
        print(f"no injection telemetry in the last {out['hours']}h "
              "(hooks log from their next run).")
        return 0
    print(f"injection telemetry — last {out['hours']}h, {out['events']} event(s)\n")
    print(f"  {'hook':10} {'calls':>6} {'bytes':>9} {'avg':>8} {'~tokens':>9}")
    for hook, h in sorted(out["per_hook"].items()):
        print(f"  {hook:10} {h['calls']:>6} {h['bytes']:>9} {h['avg_bytes']:>8} {h['est_tokens']:>9}")
    sessions = out["per_session"]
    if sessions:
        print(f"\n  sessions: {len(sessions)}, "
              f"avg ~{sum(s['est_tokens'] for s in sessions.values()) // len(sessions)} tokens/session")
    return 0
