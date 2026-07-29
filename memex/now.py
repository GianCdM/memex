"""now/ — WORKING MEMORY: one handoff page per project ("where we left off").

Written by `reflect` after each session ends — a cheap/fast model distills the
transcript tail into the same page so the next session picks up where the last left off.

The page is SHORT-TERM by design: current state only, overwritten freely.
Durable knowledge graduates to wiki/ via synth — this file is the bridge
between one session and the next, not an archive.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import limits as limits_mod
from . import providers
from . import vault as vault_mod

NOW_PROMPT = """You write the WORKING-MEMORY handoff page for someone's ongoing work — management,
architecture, tech-leadership or coding alike — the page a fresh AI session reads FIRST
to continue exactly where the previous session left off.

From the RAW session transcript below, output ONLY a Markdown body with these sections
(keep the content's own language — Portuguese/English as written):

## Contexto
1-3 sentences: what is being worked on and why.

## Estado atual
What was done / decided in THIS session; where things stand right now. Be specific.

## Próximos passos
The concrete next actions, most important first, as a `- [ ]` checklist — include
commitments (who promised what, by when) if any were made.

## Arquivos-chave
Bullets: the file paths / wiki pages / docs / people that matter right now, each with
a one-line why. OMIT this section if none.

## Threads abertos
Unresolved questions, blockers, anything waiting on someone. OMIT this section if none.

Rules:
- This is SHORT-TERM memory: the CURRENT state only — no history lessons, no chat
  transcription, no praise/filler.
- The END of the transcript is the freshest state; weight it accordingly.
- <= 60 lines total. No preamble, no frontmatter, no H1 — start at "## Contexto".

RAW SESSION (project={project}; tail of the conversation):
{raw}
"""

_KEBAB_RE = re.compile(r"[^a-z0-9]+")


def _kebab(s):
    return _KEBAB_RE.sub("-", (s or "").lower()).strip("-")


_PROJECT_CACHE = {}


def project_key_detail(cwd):
    """(slug, from_git) — the 'project' a path belongs to. from_git=True means
    the slug came from a real git repo (authoritative); False means it's just
    the folder's basename (weak — e.g. a manager running sessions from the home
    dir), in which case synth may prefer a project inferred from CONTENT."""
    if not cwd:
        return None, False
    cwd = str(cwd)
    if cwd in _PROJECT_CACHE:
        return _PROJECT_CACHE[cwd]
    proj, from_git = None, False
    try:
        path = Path(cwd)
        for d in [path, *path.parents]:
            if (d / ".git").exists():
                proj, from_git = _kebab(d.name), True
                break
        if not proj:
            proj = _kebab(path.name) or None
    except Exception:
        proj = None
    _PROJECT_CACHE[cwd] = (proj, from_git)
    return proj, from_git


def project_key(cwd):
    """The single key that ties sessions, docs, hubs and the now-page together."""
    return project_key_detail(cwd)[0]


def normalize_key(value):
    """A user-supplied --workspace value → a safe now/ key. A PATH (the natural
    reading — init's --workspace takes one) is resolved through project_key;
    anything else is kebab-cased. Without this, Path-joining an absolute path
    would silently write the page OUTSIDE the vault (`vault/'now'/'C:/x.md'`
    resolves to C:/x.md)."""
    if not value:
        return None
    value = str(value)
    if "/" in value or "\\" in value or ":" in value:
        return project_key(value)
    return _kebab(value) or None


def now_path(vault, project) -> Path:
    return Path(vault) / "now" / f"{project}.md"


_SESSION_SOURCES = {"claude", "cursor", "codex"}
_RAW_TAIL_MARKER = "[... beginning of raw omitted; latest excerpt follows ...]"


def _raw_candidates(vault):
    """Return session raws newest-first without reading their full bodies."""
    vault = Path(vault)

    def order_key(path):
        try:
            return (path.name[:10], path.stat().st_mtime)
        except OSError:
            return (path.name[:10], 0)

    return sorted(
        (p for p in (vault / "raw").glob("*.md")
         if "--doc--" not in p.name and "--code--" not in p.name),
        key=order_key,
        reverse=True,
    )


def _raw_candidate(vault, project):
    """Find the newest session raw for a workspace.

    Only the small frontmatter prefix is read for non-matches. This lookup is
    shared by reflect and boot so both paths agree about the latest session.
    """
    for path in _raw_candidates(vault):
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(1024)
            meta, _ = _split_frontmatter(head)
        except OSError:
            continue
        if meta.get("source") not in _SESSION_SOURCES:
            continue
        if project_key(meta.get("cwd")) != project:
            continue
        return path, meta
    return None, None


def latest_session_raw(vault, project):
    """Return the newest session body for a workspace, or ``None``."""
    path, _meta = _raw_candidate(vault, project)
    if not path:
        return None
    try:
        _, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        return body
    except OSError:
        return None


def _raw_is_fresh(meta, max_age_days):
    """True when a captured session belongs to the recent raw window."""
    try:
        stamp = datetime.fromisoformat(str((meta or {}).get("date")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
        return age_h <= float(max_age_days) * 24
    except (TypeError, ValueError, OverflowError):
        return False


def raw_is_newer_than_now(raw_path, now_meta) -> bool:
    """True when a captured session file was written after the now-page."""
    try:
        updated = datetime.fromisoformat(
            str((now_meta or {}).get("updated")).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return raw_path.stat().st_mtime > updated.timestamp()
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return False


def latest_session_raw_tail(vault, project, *, max_chars, max_age_days):
    """Return a bounded, recent raw tail plus its path for boot fallback.

    Boot normally injects the distilled now-page. This is only a safety net for
    a missing, stale, or not-yet-refreshed now-page; it never injects raw in
    full and keeps the complete file available for deliberate reading.
    """
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return None
    if max_chars <= 0:
        return None
    path, meta = _raw_candidate(vault, project)
    if not path or not _raw_is_fresh(meta, max_age_days):
        return None

    # UTF-8 is variable-width. Read a bounded byte window, decode safely, and
    # apply the final character cap after decoding so boot stays predictable.
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            window = max(4096, max_chars * 4 + 1024)
            fh.seek(max(0, size - window))
            text = fh.read().decode("utf-8", errors="ignore")
        if size <= window:
            _, text = _split_frontmatter(text)
        text = text.strip()
        if not text:
            return None
        if len(text) > max_chars:
            if max_chars > len(_RAW_TAIL_MARKER) + 2:
                keep = max_chars - len(_RAW_TAIL_MARKER) - 2
                text = _RAW_TAIL_MARKER + "\n\n" + text[-keep:]
            else:
                text = text[-max_chars:]
        return {"path": path, "body": text, "meta": meta}
    except OSError:
        return None


def read_now(vault, project):
    """(meta, body) of the project's now-page, or (None, None)."""
    p = now_path(vault, project)
    if not p.is_file():
        return None, None
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    return _split_frontmatter(text)


def _split_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = {}
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def write_now(vault, project, body, *, author, session_id=None) -> Path:
    """Overwrite the project's now-page (short-term memory is rewritten, not
    accumulated). Appends one line to the vault log."""
    vault = Path(vault)
    body = (body or "").strip()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = (
        "---\n"
        f"workspace: {project}\n"
        f"updated: {ts}\n"
        f"author: {author}\n"
        + (f"session: {session_id}\n" if session_id else "")
        + "---\n\n"
    )
    p = now_path(vault, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm + body + "\n", encoding="utf-8")
    vault_mod.log_append(vault, f"now/{project} updated ({author})")
    return p


def generate(vault, project, raw_text, *, provider=None) -> str:
    """Distill a transcript tail into the handoff body via the cheap/fast model.
    Raises providers.ProviderError on failure (caller decides how to degrade)."""
    lim = limits_mod.load(vault)
    vcfg = config_mod.load_vault(Path(vault))
    name, kind, settings = config_mod.resolve_provider(provider, vault_cfg=vcfg)
    model = settings.get("model_propose") or settings.get("model_merge")
    if not model:
        raise providers.ProviderError(f"no model configured for provider '{name}'")
    tail = (raw_text or "")[-lim["now_source_chars"]:]
    body = providers.complete(
        NOW_PROMPT.format(project=project, raw=tail),
        kind=kind, model=model, settings=settings)
    body = _sanitize_body(body, lim["now_max_chars"])
    if not body:
        raise providers.ProviderError("empty now-page body from provider")
    return body


def _sanitize_body(body, max_chars):
    """Model output → clean body: strip fences/frontmatter/preamble, cap size."""
    body = (body or "").strip()
    body = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", body).strip()
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:].lstrip("\n")
    i = body.find("## ")
    if i > 0:
        body = body[i:]  # drop any preamble before the first section
    return body[:max_chars].strip()


