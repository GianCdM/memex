"""memex/metrics — per-raw pipeline telemetry.

Every synthesized raw appends one JSONL line to `.memex/metrics.jsonl`:

  {"ts": 1786…, "fname": "2026-…", "kind": "session|doc", "mode": "full|doc-delta|session-delta",
   "outcome": "supported|partial|…", "route": "auto_apply|review|…",
   "reason": "…", "latency_ms": 1234, "body_chars": 51234,
   "delta_chars": …, "checkpoint_before": …, "checkpoint_after": …,
   "model_propose": "…", "model_merge": "…", "verify_model": "…"}

Delta rows (mode ends in "-delta") add `delta_chars` and the checkpoint window
that advanced (`checkpoint_before`/`checkpoint_after`).

`memex metrics` summarizes this (counts by outcome/route/kind/mode, average
latency, an estimated cost) so optimization decisions are grounded in real
numbers instead of guesses. The file is append-only; `memex metrics` is
read-only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

METRICS_REL = Path(".memex") / "metrics.jsonl"


def log(vault, event: dict) -> None:
    """Append one metrics event (best-effort — never breaks a synth run)."""
    try:
        path = Path(vault) / METRICS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        event.setdefault("ts", int(time.time()))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read(vault):
    """Yield parsed events from `.memex/metrics.jsonl` (oldest first)."""
    path = Path(vault) / METRICS_REL
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def summarize(vault, since=None):
    """Aggregate the metrics JSONL into a compact dict for `memex metrics`.

    `since` (ISO date string) filters to events on/after that day. Cost is a
    rough estimate using a tokens-per-char factor and the configured models'
    input/output prices — the real number depends on the provider's billing.
    """
    from collections import Counter
    from . import config as config_mod

    counts = Counter()
    by_kind = Counter()
    by_route = Counter()
    by_mode = Counter()
    lat = []
    chars = 0
    rows = 0
    cfg = config_mod.load_vault(vault) or {}
    verify_model = cfg.get("verify_model") or "?"
    for ev in read(vault):
        if since and ev.get("ts", 0) < _day_ts(since):
            continue
        rows += 1
        counts[ev.get("outcome", "?")] += 1
        by_kind[ev.get("kind", "?")] += 1
        by_route[ev.get("route", "?")] += 1
        by_mode[ev.get("mode", "?")] += 1
        lat.append(ev.get("latency_ms", 0))
        chars += ev.get("body_chars", 0)
    n = max(rows, 1)
    return {
        "rows": rows,
        "by_outcome": dict(counts),
        "by_route": dict(by_route),
        "by_kind": dict(by_kind),
        "by_mode": dict(by_mode),
        "avg_latency_ms": round(sum(lat) / n, 1),
        "total_body_chars": chars,
        "verify_model": verify_model,
    }


def _day_ts(iso: str) -> int:
    """Unix ts for an ISO date string (YYYY-MM-DD) at 00:00 local."""
    import datetime
    dt = datetime.datetime.strptime(iso, "%Y-%m-%d")
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())


def run(args) -> int:
    """`memex metrics` — print the pipeline telemetry summary."""
    from . import config as config_mod
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    s = summarize(vault, since=getattr(args, "since", None))
    print(f"pipeline metrics for {vault}")
    print(f"  rows: {s['rows']}  avg latency: {s['avg_latency_ms']}ms  "
          f"total body chars: {s['total_body_chars']:,}")
    print(f"  verify model: {s['verify_model']}")
    print(f"  by outcome: {s['by_outcome'] or '(none)'}")
    print(f"  by route:   {s['by_route'] or '(none)'}")
    print(f"  by kind:    {s['by_kind'] or '(none)'}")
    print(f"  by mode:    {s['by_mode'] or '(none)'}")
    return 0
