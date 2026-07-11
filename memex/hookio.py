"""Hook I/O — read the harness payload and keep tiny per-session state.

Claude Code passes hook input as JSON on stdin (session_id, transcript_path,
cwd, source/prompt/...). Everything here is defensive: a hook must NEVER
raise, block, or slow the session down, so all readers degrade to {} / no-op.

Per-session state lives in <vault>/.memex/state/ — e.g. which wiki pages were
already injected into a session (so recall never repeats itself). State files
are throwaway and pruned by age.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def read_payload() -> dict:
    """The hook JSON from stdin, or {} (tty, empty, malformed — never raises)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def state_dir(vault: Path) -> Path:
    d = Path(vault) / ".memex" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name))[:80]


def load_state(vault, name) -> dict:
    try:
        return json.loads((state_dir(vault) / f"{_sanitize(name)}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(vault, name, data: dict) -> None:
    try:
        payload = dict(data)
        payload["_ts"] = int(time.time())
        (state_dir(vault) / f"{_sanitize(name)}.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )
    except Exception:
        pass


def clear_state(vault, name) -> None:
    try:
        (state_dir(vault) / f"{_sanitize(name)}.json").unlink()
    except Exception:
        pass


def prune_state(vault, max_age_days=7, prefix="recall-") -> None:
    """Drop SESSION-scoped state files older than max_age_days. Scoped by
    prefix on purpose: durable markers (e.g. last-tidy, which legitimately
    ages past a week) share this directory and must survive. Best-effort."""
    try:
        cutoff = time.time() - max_age_days * 86400
        for f in state_dir(vault).glob(f"{prefix}*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except Exception:
        pass
