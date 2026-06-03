"""Session-source backends for memex ingest.

Each backend reads OLD *local* AI-coding sessions from one tool and yields a
uniform session dict, so the ingest flow never changes per source. Ported from
the user's `session-recall` skill (search/extract + on-disk format handling).

Every backend module exposes exactly:

    available() -> bool
    iter_sessions(workspace=None, since=None) -> Iterator[dict]

and each yielded dict has the shape::

    {
        "source": "claude" | "cursor" | "codex",
        "id":     "<raw session id>",
        "title":  "<short title, or first user line truncated>",
        "date":   "<ISO8601 timestamp, best-effort>",
        "cwd":    "<absolute workspace path, or None>",
        "text":   "<clean conversation as markdown>",
    }

Stdlib only. Backends never crash on a malformed session — they skip it.
"""

from __future__ import annotations

from . import claude, codex, cursor

BACKENDS = {"claude": claude, "cursor": cursor, "codex": codex}


def iter_all(sources=None, workspace=None, since=None):
    """Iterate sessions across the given source names.

    `sources` defaults to every backend whose local storage exists on the
    machine (`available()`). Unknown names are ignored; a backend that raises is
    skipped so one broken store never kills the rest.
    """
    if sources is None:
        names = [name for name, mod in BACKENDS.items() if _safe_available(mod)]
    else:
        names = [name for name in sources if name in BACKENDS]
    for name in names:
        mod = BACKENDS[name]
        try:
            yield from mod.iter_sessions(workspace=workspace, since=since)
        except Exception:
            # A broken backend must not kill the others.
            continue


def _safe_available(mod) -> bool:
    try:
        return bool(mod.available())
    except Exception:
        return False
