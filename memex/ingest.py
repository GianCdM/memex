"""memex ingest — capture sessions / codebase / docs into raw/ (LLM-free).

Idempotent: a ledger (.memex/ledger.jsonl) keyed by source:id:content-hash means
re-running only writes what changed. Scrubs secrets before writing. The raw note
is the cleaned conversation/file (forensic layer); synth turns it into the wiki.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import extract as extract_mod
from . import resolve as resolve_mod
from . import scrub as scrub_mod

# config/data exts (.yaml/.yml/.json/.toml) are intentionally EXCLUDED — config -> skip.
# (and code is meant to go through `memex analyze`, not this legacy per-file path.)
CODE_SIGNAL_EXT = {".md", ".rst", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx",
                   ".go", ".rs", ".java", ".kt", ".rb", ".sql", ".sh", ".tf"}
CODE_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
                  "__pycache__", ".next", "target", ".idea", ".vscode", ".mypy_cache"}
# accepted content types for `ingest --docs` live in extract.CONTENT_EXT (text +
# documents + images + audio + video; binaries are refused). Binary docs/media are
# auto-extracted to text via the best local tool — see memex/extract.py.


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _slugify(s, maxlen=40):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:maxlen] or "untitled")


def _ledger_load(vault):
    seen = set()
    p = vault / ".memex" / "ledger.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except Exception:
                pass
    return seen


def _ledger_append(vault, key, fname):
    with (vault / ".memex" / "ledger.jsonl").open("a") as f:
        f.write(json.dumps({"key": key, "raw": fname, "ts": int(time.time())}) + "\n")


def _write_raw(vault, *, source, sid, date, cwd, tier, text):
    raw_dir = vault / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    datepart = (date or "")[:10] or "0000-00-00"
    uniq = hashlib.sha256(str(sid).encode()).hexdigest()[:8]
    fname = f"{datepart}--{source}--{_slugify(sid, 32)}--{uniq}.md"
    fm = (
        "---\n"
        f"source: {source}\n"
        f"id: {sid}\n"
        f"date: {date or ''}\n"
        f"cwd: {cwd or ''}\n"
        f"tier: {tier}\n"
        "---\n\n"
    )
    (raw_dir / fname).write_text(fm + scrub_mod.scrub(text or "").rstrip() + "\n")
    return fname


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1

    seen = _ledger_load(vault)
    total = 0
    did_something = False

    if getattr(args, "codebase", None) is not None:
        total += _ingest_codebase(vault, args, seen)
        did_something = True
    if getattr(args, "doc", None):
        total += _ingest_doc(vault, args, seen)
        did_something = True
    if getattr(args, "docs", None):
        total += _ingest_docs(vault, args, seen)
        did_something = True
    if getattr(args, "index", None):
        total += _ingest_index(vault, args, seen)
        did_something = True
    if getattr(args, "all", False) or getattr(args, "session", None):
        total += _ingest_sessions(vault, args, seen)
        did_something = True

    if not did_something:
        print("nothing to do: pass --all (sessions), --docs <dir|glob>, --doc <file>, or --codebase [path].")
        return 1
    print(f"\n✓ ingest done. {total} new raw note(s).")
    return 0


def _ingest_sessions(vault, args, seen):
    from . import sources  # provided by memex/sources (parser package)

    src_names = None
    if getattr(args, "source", None) and args.source != "auto":
        src_names = [args.source]
    workspace = getattr(args, "workspace", None)
    since = getattr(args, "since", None)
    n = 0
    print("ingesting sessions...")
    for sess in sources.iter_all(sources=src_names, workspace=workspace, since=since):
        text = sess.get("text") or ""
        key = f"{sess['source']}:{sess['id']}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if key in seen:
            continue
        fname = _write_raw(
            vault, source=sess["source"], sid=sess["id"], date=sess.get("date"),
            cwd=sess.get("cwd"), tier="silver", text=text)
        _ledger_append(vault, key, fname)
        seen.add(key)
        n += 1
        print(f"  + {sess['source']}:{str(sess['id'])[:12]} -> {fname}")
    print(f"  sessions: {n} new")
    return n


def _list_repo_files(root):
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return [root / line for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    files = []
    for p in root.rglob("*"):
        if p.is_file() and not any(d in p.parts for d in CODE_SKIP_DIRS):
            files.append(p)
    return files


def _ingest_codebase(vault, args, seen):
    root = Path(args.codebase or ".").expanduser().resolve()
    tier = getattr(args, "tier_override", None) or "gold"
    if not root.exists():
        print(f"  codebase path not found: {root}")
        return 0
    n = 0
    print(f"ingesting codebase {root} (tier={tier}, respecting .gitignore)...")
    for fp in _list_repo_files(root):
        if fp.suffix.lower() not in CODE_SIGNAL_EXT:
            continue
        try:
            text = fp.read_text(errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        rel = str(fp.relative_to(root))
        key = f"code:{root.name}:{rel}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if key in seen:
            continue
        note = f"# {root.name}/{rel}\n\n```\n{text[:8000]}\n```\n"
        fname = _write_raw(vault, source="code", sid=f"{root.name}/{rel}",
                           date=_today(), cwd=str(root), tier=tier, text=note)
        _ledger_append(vault, key, fname)
        seen.add(key)
        n += 1
    print(f"  codebase: {n} signal file(s)")
    return n


def _ingest_doc(vault, args, seen):
    fp = Path(args.doc).expanduser().resolve()
    if not fp.exists():
        print(f"  doc not found: {fp}")
        return 0
    tier = getattr(args, "tier_override", None) or "silver"
    text = fp.read_text(errors="ignore")
    key = f"doc:{fp}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    if key in seen:
        print(f"  doc unchanged, skipped: {fp.name}")
        return 0
    fname = _write_raw(vault, source="doc", sid=fp.name, date=_today(),
                       cwd=str(fp.parent), tier=tier, text=text)
    _ledger_append(vault, key, fname)
    print(f"  + doc {fp.name} -> {fname}")
    return 1


def _resolve_doc_files(spec):
    """`spec` is a directory (recurse for prose files) or a glob pattern."""
    import glob as _glob
    p = Path(spec).expanduser()
    out = []
    if p.is_dir():
        for fp in p.rglob("*"):
            if (fp.is_file() and fp.suffix.lower() in extract_mod.CONTENT_EXT
                    and not any(d in fp.parts for d in CODE_SKIP_DIRS)):
                out.append(fp)
    else:  # treat as a glob (supports ** with recursive=True)
        for m in _glob.glob(str(p), recursive=True):
            fp = Path(m)
            if fp.is_file() and fp.suffix.lower() in extract_mod.CONTENT_EXT:
                out.append(fp)
    return sorted({fp.resolve() for fp in out})  # canonical paths → stable dedup


def _ingest_docs(vault, args, seen):
    """Bulk-adopt a folder/glob of documents & media. Binaries (pdf/docx/pptx/
    images/audio/video) are EXTRACTED to text via the best local tool; non-content
    binaries are refused; missing tools skip gracefully with a hint."""
    files = _resolve_doc_files(args.docs)
    if not files:
        print(f"  no content files matched: {args.docs}")
        return 0
    tier = getattr(args, "tier_override", None) or "silver"
    n, skipped = 0, 0
    print(f"ingesting docs/media: {args.docs} ({len(files)} file(s))...")
    for fp in files:
        text, method = extract_mod.extract(fp)
        if not text or not text.strip():
            print(f"  - skip {fp.name}: {method}")
            skipped += 1
            continue
        # full path as id -> unique per file (no collision across same-named files)
        key = f"doc:{fp}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if key in seen:
            continue
        fname = _write_raw(vault, source="doc", sid=str(fp), date=_today(),
                           cwd=str(fp.parent), tier=tier, text=text)
        _ledger_append(vault, key, fname)
        seen.add(key)
        n += 1
        if method != "text":
            print(f"  + {fp.name}  (extracted via {method})")
    print(f"  docs/media: {n} new" + (f", {skipped} skipped" if skipped else ""))
    return n


def _ingest_index(vault, args, seen):
    """Ingest from a doc index (jsonl of locators) — see memex/resolve.py. Resolves
    each entry to text (description / filesystem / provider-MCP), adopts it, and
    SKIPS sensitive (PII) entries by default."""
    entries = resolve_mod.read_index(args.index)
    if not entries:
        print(f"  no entries in index: {args.index}")
        return 0
    allow_mcp = getattr(args, "index_mcp", False)
    prov = None
    if allow_mcp:
        try:
            from . import config as config_mod
            _, kind, settings = config_mod.resolve_provider(
                getattr(args, "provider", None), vault_cfg=config_mod.load_vault(vault))
            prov = {"kind": kind, "model": settings.get("model_merge"), "settings": settings,
                    "mcp_server": getattr(args, "index_mcp_server", None)}
        except Exception:
            prov = None
    tier = getattr(args, "tier_override", None) or "silver"
    idx_path = Path(args.index).expanduser().resolve()
    idx_dir = str(idx_path.parent)
    # content root for entries' relative `path`: explicit --index-base, else probe the
    # dirs near the index (so an index tucked in a subfolder still finds its files).
    base = getattr(args, "index_base", None)
    base = (Path(base).expanduser().resolve() if base
            else resolve_mod.probe_base(entries, [idx_path.parent, idx_path.parent.parent]))
    n = skipped = sensitive = 0
    print(f"ingesting index: {args.index} ({len(entries)} entries"
          + (f", base={base}" if base else "")
          + (", MCP via provider" if allow_mcp else "") + ")...")
    for e in entries:
        text, method = resolve_mod.resolve_entry(e, base=base, allow_mcp=allow_mcp, prov=prov)
        if not text:
            sensitive += "sensitive" in method
            skipped += "sensitive" not in method
            continue
        sid = e.get("path") or e.get("drive_id") or "entry"
        key = f"doc:index:{sid}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if key in seen:
            continue
        fname = _write_raw(vault, source="doc", sid=str(sid), date=_today(),
                           cwd=idx_dir, tier=tier, text=text)
        _ledger_append(vault, key, fname)
        seen.add(key)
        n += 1
        print(f"  + {str(sid)[:46]:48} ({method})")
    tail = f"  index: {n} new"
    if sensitive:
        tail += f", {sensitive} sensitive skipped"
    if skipped:
        tail += f", {skipped} unresolved"
    print(tail)
    return n
