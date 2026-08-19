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

# code goes through `memex analyze` (architecture synthesis) — never file-by-file:
# the SCHEMA's rule is "reference code by repo path, don't duplicate what git owns".
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
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except Exception:
                pass
    return seen


def _ledger_append(vault, key, fname):
    with (vault / ".memex" / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "raw": fname, "ts": int(time.time())}) + "\n")


def _write_raw(vault, *, source, sid, date, cwd, kind, text, extra_meta=None,
               identity=""):
    """Write immutable evidence with optional capture provenance.

    ``identity`` distinguishes two transcript windows whose cleaned text happens
    to be equal. The content hash remains the hash of the source text; filename
    uniqueness additionally covers the non-sensitive capture window identity.
    """
    from . import canon as canon_mod
    raw_dir = canon_mod.raw_dir(vault)
    raw_dir.mkdir(parents=True, exist_ok=True)
    datepart = (date or "")[:10] or "0000-00-00"
    content_hash = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    file_hash = hashlib.sha256(((text or "") + "\0" + str(identity or "")).encode("utf-8")).hexdigest()
    short_hash = file_hash[:12]
    fname = f"{datepart}--{source}--{_slugify(sid, 32)}--{short_hash}.md"
    target = raw_dir / fname
    if target.exists():
        return fname  # content-identical raw already captured — never rewrite evidence
    pii_found = scrub_mod.detect_pii(text or "")
    scrubbed = scrub_mod.scrub(text or "").rstrip()
    lines = [
        "---", f"source: {source}", f"id: {sid}", f"date: {date or ''}",
        f"cwd: {cwd or ''}", f"kind: {kind}",
    ]
    for key, value in (extra_meta or {}).items():
        if value is not None and str(value) != "":
            lines.append(f"{key}: {value}")
    if pii_found:
        lines.append(f"pii: {', '.join(pii_found)}")
    lines.extend([f"content_sha256: {content_hash}", "---", ""])
    target.write_text("\n".join(lines) + scrubbed + "\n", encoding="utf-8")
    if pii_found:
        print(f"  ⚠ pii redacted: {', '.join(pii_found)}  (saved -> raw/{fname})")
    return fname


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1

    seen = _ledger_load(vault)
    total = 0
    did_something = False

    # --doc <file> is sugar for the --docs pipeline (same extraction, same
    # stat+content gates) — the old separate path read binaries as sludge
    specs = [s for s in (getattr(args, "docs", None), getattr(args, "doc", None)) if s]
    for spec in specs:
        total += _ingest_docs(vault, args, seen, spec)
        did_something = True
    if getattr(args, "index", None):
        total += _ingest_index(vault, args, seen)
        did_something = True
    if getattr(args, "all", False) or getattr(args, "session", None):
        total += _ingest_sessions(vault, args, seen)
        did_something = True

    if not did_something:
        print("nothing to do: pass --all (sessions), --docs <dir|glob>, or --doc <file>.")
        return 1
    print(f"\n✓ ingest done. {total} new raw note(s).")
    return 0


def ingest_session(vault, sess, seen, kind="session", *, extra_meta=None,
                   identity=""):
    """Write ONE session dict (from memex/sources) into raw/, idempotently.

    ``extra_meta``/``identity`` are used by hook-only transcript windows; bulk
    scans keep the historic full-snapshot behaviour unchanged.
    """
    text = (sess or {}).get("text") or ""
    if not text.strip():
        return None
    body_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
    key = f"{sess['source']}:{sess['id']}:{body_hash}:{identity}" if identity else \
          f"{sess['source']}:{sess['id']}:{body_hash}"
    if key in seen:
        return None
    fname = _write_raw(
        vault, source=sess["source"], sid=sess["id"], date=sess.get("date"),
        cwd=sess.get("cwd"), kind=kind, text=text, extra_meta=extra_meta,
        identity=identity)
    _ledger_append(vault, key, fname)
    seen.add(key)
    return fname


def _ingest_sessions(vault, args, seen):
    from . import sources  # provided by memex/sources (parser package)
    from . import ui

    src_names = None
    if getattr(args, "source", None) and args.source != "auto":
        src_names = [args.source]
    workspace = getattr(args, "workspace", None)
    since = getattr(args, "since", None)
    n = 0
    print("ingesting sessions...")
    with ui.Progress("  scanning sessions") as bar:
        for sess in sources.iter_all(sources=src_names, workspace=workspace, since=since):
            fname = ingest_session(vault, sess, seen, kind="session")
            if fname:
                n += 1
                if not bar.enabled:
                    print(f"  + {sess['source']}:{str(sess['id'])[:12]} -> {fname}")
            bar.update(suffix=f"({n} new)")
    print(f"  sessions: {n} new")
    return n


# obvious backup / old copies — skipped so the wiki isn't polluted with
# near-duplicate versions of the same doc (e.g. foo.backup.pptx, foo.pptx.bak-…)
_BACKUP_RE = re.compile(r"(?i)(?:\.(?:backup|bak|orig|old)\b|~$)")


def _doc_filters(vault):
    """Per-vault ingest allowlist/denylist from .memex/config.json.

    Shape (all values are glob lists, matched against the resolved file path;
    `*` crosses directory separators, so `**/*.log` works):
      { "ingest": { "docs": {
          "include": ["docs/**/*.md", "README.md"],   # if set, ONLY these
          "exclude": ["**/*.log", "pessoal/automation/**"],  # never these
          "skip_ids": ["**/morning-routine.log"] } } }  # index entries too
    Empty/missing = keep the legacy behavior (adopt every prose file).
    """
    try:
        from . import config as config_mod
        cfg = config_mod.load_vault(vault) or {}
        return dict((cfg.get("ingest") or {}).get("docs") or {})
    except Exception:
        return {}


def _matches_any(fp: Path, globs):
    import fnmatch
    if not globs:
        return False
    s = str(fp.resolve())
    return any(fnmatch.fnmatch(s, g) for g in globs)


def _resolve_doc_files(spec, exclude=None, include=None, exclude_globs=None):
    """`spec` is a directory (walk for prose files, PRUNING skip-dirs and
    dot-dirs during traversal — an rglob would stat every file in node_modules
    first) or a glob pattern. Skips backup/old copies and anything under
    `exclude` (the vault itself, so a vault inside the workspace is never
    self-ingested). `include`/`exclude_globs` are per-vault doc filters from
    `_doc_filters`: `include` (non-empty) restricts to matching files, and
    `exclude_globs` drops matching files on top of every other gate."""
    import glob as _glob
    import os as _os

    exclude = str(Path(exclude).resolve()) if exclude else None

    def _excluded(fp: Path) -> bool:
        if not exclude:
            return False
        try:
            return str(fp.resolve()).lower().startswith(exclude.lower() + _os.sep) \
                or str(fp.resolve()).lower() == exclude.lower()
        except OSError:
            return True

    def _ok(fp):
        if include and not _matches_any(fp, include):
            return False
        return (fp.is_file() and fp.suffix.lower() in extract_mod.CONTENT_EXT
                and not any(d in fp.parts for d in CODE_SKIP_DIRS)
                and not _BACKUP_RE.search(fp.name)
                and not _excluded(fp)
                and not _matches_any(fp, exclude_globs))

    p = Path(spec).expanduser()
    out = []
    if p.is_dir():
        for dirpath, dirnames, filenames in _os.walk(p):
            # prune during the walk: skip-dirs, dot-dirs, and the vault
            dirnames[:] = [d for d in dirnames
                           if d not in CODE_SKIP_DIRS and not d.startswith(".")
                           and not _excluded(Path(dirpath) / d)]
            for name in filenames:
                fp = Path(dirpath) / name
                if _ok(fp):
                    out.append(fp)
    else:  # treat as a glob (supports ** with recursive=True)
        out = [Path(m) for m in _glob.glob(str(p), recursive=True) if _ok(Path(m))]
    return sorted({fp.resolve() for fp in out})  # canonical paths → stable dedup


def _ingest_docs(vault, args, seen, spec=None):
    """Bulk-adopt a folder/glob of documents & media. Binaries (pdf/docx/pptx/
    images/audio/video) are EXTRACTED to text via the best local tool; non-content
    binaries are refused; missing tools skip gracefully with a hint.

    Two-level dedup gate (same design as the doc-index path): a cheap stat key
    (path:mtime:size) skips unchanged files without extracting, and a CONTENT
    hash skips files whose mtime churned but whose text didn't (git checkout,
    re-clone, sync tools) — otherwise every branch switch would re-write raw
    notes and burn 2 LLM calls per doc re-synthesizing identical pages."""
    spec = spec or args.docs
    filters = _doc_filters(vault)
    files = _resolve_doc_files(spec, exclude=getattr(args, "exclude", None),
                               include=filters.get("include"),
                               exclude_globs=filters.get("exclude"))
    if not files:
        print(f"  no content files matched: {spec}"
              + (f" (include={filters.get('include')})" if filters.get("include") else ""))
        return 0
    from . import ui
    kind = "doc"
    n, skipped, unchanged = 0, 0, 0
    print(f"ingesting docs/media: {spec} ({len(files)} file(s))...")
    bar = ui.Progress("  docs", total=len(files))
    for fp in files:
        bar.update(suffix=fp.name[:32])
        # gate 1: cheap stat (path:mtime:size) BEFORE the (costly) extraction —
        # re-running over an unchanged tree must not re-run pandoc/OCR per file.
        try:
            st = fp.stat()
        except OSError:
            continue
        stat_key = f"doc:{fp}:{int(st.st_mtime)}:{st.st_size}"
        if stat_key in seen:
            unchanged += 1
            continue
        text, method = extract_mod.extract(fp)
        if not text or not text.strip():
            if not bar.enabled:
                print(f"  - skip {fp.name}: {method}")
            # remember PERMANENT refusals only (non-content binaries). A
            # missing-extractor skip stays UN-ledgered so the file is retried
            # on the next run — installing markitdown later must pick up every
            # previously-skipped pdf/xlsx without an mtime dance.
            if (method or "").startswith("refused"):
                _ledger_append(vault, stat_key, "")
                seen.add(stat_key)
            skipped += 1
            continue
        # gate 2: content hash — mtime changed but the text didn't
        content_key = f"doc:{fp}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if content_key in seen:
            _ledger_append(vault, stat_key, "")  # remember the new stat, skip the rewrite
            seen.add(stat_key)
            unchanged += 1
            continue
        fname = _write_raw(vault, source="doc", sid=str(fp), date=_today(),
                           cwd=str(fp.parent), kind=kind, text=text)
        for key in (stat_key, content_key):
            _ledger_append(vault, key, fname)
            seen.add(key)
        n += 1
        if method != "text" and not bar.enabled:
            print(f"  + {fp.name}  (extracted via {method})")
    bar.done()
    tail = f"  docs/media: {n} new"
    if unchanged:
        tail += f", {unchanged} unchanged"
    if skipped:
        tail += f", {skipped} skipped"
    print(tail)
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
            models = config_mod.resolve_models(vault_cfg=config_mod.load_vault(vault))
            prov = {"model": models.get("merge") or models.get("propose"),
                    "settings": {},
                    "mcp_server": getattr(args, "index_mcp_server", None)}
        except Exception:
            prov = None
    kind = "doc"
    idx_path = Path(args.index).expanduser().resolve()
    idx_dir = str(idx_path.parent)
    # content root for entries' relative `path`: explicit --index-base, else probe the
    # dirs near the index (so an index tucked in a subfolder still finds its files).
    base = getattr(args, "index_base", None)
    base = (Path(base).expanduser().resolve() if base
            else resolve_mod.probe_base(entries, [idx_path.parent, idx_path.parent.parent]))
    n = skipped = sensitive = unchanged = 0
    print(f"ingesting index: {args.index} ({len(entries)} entries"
          + (f", base={base}" if base else "")
          + (", MCP via provider" if allow_mcp else "") + ")...")
    skip_ids = _doc_filters(vault).get("skip_ids") or []
    for e in entries:
        sid = e.get("path") or e.get("drive_id") or "entry"
        # per-vault skip_ids: drop entries whose locator matches (e.g. a
        # personal automation log that should never become a wiki page).
        if skip_ids and _matches_any(Path(str(sid)), skip_ids):
            skipped += 1
            continue
        # fingerprint gate: skip (re)resolution — including a slow MCP fetch — when the
        # index says this entry is unchanged since we last resolved it.
        fpx = e.get("fingerprint")
        pre_key = f"idx:{sid}:{fpx}" if fpx else None
        if pre_key and pre_key in seen:
            unchanged += 1
            continue
        text, method = resolve_mod.resolve_entry(e, base=base, allow_mcp=allow_mcp, prov=prov)
        if not text:
            is_pii = "sensitive" in method
            sensitive += is_pii
            skipped += not is_pii
            if pre_key and is_pii:  # PII is a stable by-design skip — don't re-resolve next run
                _ledger_append(vault, pre_key, "")
                seen.add(pre_key)
            continue
        key = f"doc:index:{sid}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if key not in seen:
            fname = _write_raw(vault, source="doc", sid=str(sid), date=_today(),
                               cwd=idx_dir, kind=kind, text=text)
            _ledger_append(vault, key, fname)
            seen.add(key)
            n += 1
            print(f"  + {str(sid)[:46]:48} ({method})")
        if pre_key:  # remember fingerprint → an unchanged entry skips resolution next run
            _ledger_append(vault, pre_key, "")
            seen.add(pre_key)
    tail = f"  index: {n} new"
    if unchanged:
        tail += f", {unchanged} unchanged"
    if sensitive:
        tail += f", {sensitive} sensitive skipped"
    if skipped:
        tail += f", {skipped} unresolved"
    print(tail)
    return n
