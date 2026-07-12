"""memex remember — deliberately file one durable fact into the brain.

The write path for "lembra disso": the text lands in raw/ like any capture
(scrubbed, ledgered) and is then synthesized INLINE — just that one note — so
the fact is a wiki page seconds later, not "eventually". If the provider is
down the note simply stays pending and the next reflect picks it up; the fact
is never lost.

Used by humans and by Claude (via the memex skill) alike.
"""

from __future__ import annotations

import sys
import time
from argparse import Namespace
from pathlib import Path

from . import config as config_mod
from . import ingest as ingest_mod
from . import synth as synth_mod
from . import vault as vault_mod


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1

    text = " ".join(getattr(args, "text", None) or []).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        print('usage: memex remember "<one clear paragraph worth keeping>"')
        return 1

    cwd = str(Path.cwd())
    sid = f"remember-{int(time.time())}"
    seen = ingest_mod._ledger_load(vault)
    fname = ingest_mod.ingest_session(
        vault,
        {"source": "remember", "id": sid, "date": time.strftime("%Y-%m-%d"),
         "cwd": cwd, "text": text},
        seen,
    )
    if not fname:
        print("nothing saved (empty or already known).")
        return 0
    vault_mod.log_append(vault, f"remember: {text[:80]}")
    print(f"✓ saved -> raw/{fname}")

    # compile it into the wiki right now (just this note; provider may be down
    # or the vault busy). The truth of "did it compile" is synthed.json — the
    # return code alone can't tell (a lock-skip and a single provider error
    # both return 0).
    synth_mod.run(Namespace(
        vault=str(vault), provider=getattr(args, "provider", None),
        limit=None, since=None, only=fname,
        model_propose=None, model_merge=None,
    ))
    import json
    try:
        synthed = json.loads((vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
    except Exception:
        synthed = {}
    if fname not in synthed:
        print("  (synthesis pending — the next reflect will compile it)")
    return 0
