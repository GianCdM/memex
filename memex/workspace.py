"""workspace/ — short-term handoffs keyed by collision-safe workspace identities."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from . import canon as canon_mod
from . import config as config_mod
from . import contracts as ctr
from . import limits as limits_mod
from . import providers
from . import vault as vault_mod
from . import verify as verify_mod

WORKSPACE_PROMPT = """You write the WORKING-MEMORY handoff page for someone's ongoing work — management,
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

CURRENT WORKSPACE PAGE:
{current}

NEW TRANSCRIPT CONTENT SINCE THE LAST WORKSPACE CHECKPOINT:
{raw}
"""

_KEBAB_RE = re.compile(r"[^a-z0-9]+")
_PROJECT_CACHE = {}
_WORKSPACE_CACHE = {}
_SESSION_SOURCES = {"claude"}
_RAW_TAIL_MARKER = "[... beginning of raw omitted; latest excerpt follows ...]"


def _kebab(value):
    return _KEBAB_RE.sub("-", str(value or "").lower()).strip("-")


def _git_root(path):
    """Return the nearest Git root, including nested repositories/worktrees."""
    for directory in (path, *path.parents):
        if (directory / ".git").exists():
            return directory
    return None


def project_key_detail(cwd):
    """Semantic project identity used by synthesis and wiki hubs.

    It intentionally keeps the prior basename behavior. A semantic project is
    not necessarily a workspace path.
    """
    if not cwd:
        return None, False
    cwd = str(cwd)
    if cwd in _PROJECT_CACHE:
        return _PROJECT_CACHE[cwd]
    project, from_git = None, False
    try:
        path = Path(cwd).expanduser().resolve()
        root = _git_root(path)
        if root:
            project, from_git = _kebab(root.name), True
        if not project:
            project = _kebab(path.name) or None
    except Exception:
        project = None
    _PROJECT_CACHE[cwd] = (project, from_git)
    return project, from_git


def project_key(cwd):
    return project_key_detail(cwd)[0]


def workspace_key_detail(cwd):
    """Return ``(key, root, display_name)`` for a technical workspace identity.

    Git sessions resolve to their nearest repository root. Non-Git sessions use
    their actual cwd. Home-relative paths become readable hierarchical names;
    paths outside HOME receive a deterministic hash suffix to prevent collisions.
    """
    if not cwd:
        return None, None, None
    raw = str(cwd)
    if raw in _WORKSPACE_CACHE:
        return _WORKSPACE_CACHE[raw]
    key = root_text = display = None
    try:
        path = Path(raw).expanduser().resolve()
        root = _git_root(path) or path
        root_text = str(root)
        display = root.name or "workspace"
        try:
            rel = root.relative_to(Path.home().resolve())
        except ValueError:
            readable = _kebab("-".join(root.parts)) or _kebab(display) or "workspace"
            key = f"external-{readable}--{hashlib.sha256(root_text.encode()).hexdigest()[:8]}"
        else:
            key = _kebab("-".join(rel.parts)) or _kebab(display) or "workspace"
    except Exception:
        key = _kebab(Path(raw).name) or None
        root_text = raw
        display = Path(raw).name or None
    out = (key, root_text, display)
    _WORKSPACE_CACHE[raw] = out
    return out


def workspace_key(cwd):
    return workspace_key_detail(cwd)[0]


def workspace_display_name(cwd):
    return workspace_key_detail(cwd)[2]


def legacy_workspace_key(cwd):
    """The pre-hierarchical basename key, retained for migration only."""
    return project_key(cwd)


def normalize_key(value):
    if not value:
        return None
    value = str(value)
    if "/" in value or "\\" in value or ":" in value:
        return workspace_key(value)
    return _kebab(value) or None


def workspace_path(vault, workspace):
    return Path(vault) / "workspace" / f"{workspace}.md"


def _raw_candidates(vault):
    vault = Path(vault)

    def order_key(path):
        # "Latest" = most RECENT capture (last activity), not the session that
        # STARTED latest. A session captured today (mtime = now) is the active
        # workspace even when an older session's filename carries a later
        # start-date prefix — sorting by the date prefix first kept surfacing
        # a stale session's snapshot and the workspace page never advanced to
        # the session that's actually being worked on.
        try:
            return (path.stat().st_mtime, path.name[:10])
        except OSError:
            return (0, path.name[:10])

    return sorted(
        (path for path in canon_mod.raw_dir(vault).glob("*.md")
         if "--doc--" not in path.name and "--code--" not in path.name),
        key=order_key,
        reverse=True,
    )


def _split_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = {}
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def _read_raw_meta(path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return _split_frontmatter(handle.read(2048))[0]
    except OSError:
        return {}


def _raw_candidate(vault, workspace):
    for path in _raw_candidates(vault):
        meta = _read_raw_meta(path)
        if meta.get("source") not in _SESSION_SOURCES:
            continue
        if workspace_key(meta.get("cwd")) == workspace:
            return path, meta
    return None, None


def latest_session_raw(vault, workspace):
    path, _meta = _raw_candidate(vault, workspace)
    if not path:
        return None
    try:
        _, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        return body
    except OSError:
        return None


def _raw_is_fresh(meta, max_age_days):
    try:
        stamp = datetime.fromisoformat(str((meta or {}).get("date")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0 <= float(max_age_days) * 24
    except (TypeError, ValueError, OverflowError):
        return False


def raw_is_newer_than_workspace(raw_path, workspace_meta):
    try:
        updated = datetime.fromisoformat(str((workspace_meta or {}).get("updated")).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return raw_path.stat().st_mtime > updated.timestamp()
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return False


def latest_session_raw_tail(vault, workspace, *, max_chars, max_age_days):
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return None
    if max_chars <= 0:
        return None
    path, meta = _raw_candidate(vault, workspace)
    if not path or not _raw_is_fresh(meta, max_age_days):
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            window = max(4096, max_chars * 4 + 1024)
            handle.seek(max(0, size - window))
            text = handle.read().decode("utf-8", errors="ignore")
        if size <= window:
            _, text = _split_frontmatter(text)
        text = text.strip()
        if not text:
            return None
        if len(text) > max_chars:
            if max_chars > len(_RAW_TAIL_MARKER) + 2:
                text = _RAW_TAIL_MARKER + "\n\n" + text[-(max_chars - len(_RAW_TAIL_MARKER) - 2):]
            else:
                text = text[-max_chars:]
        return {"path": path, "body": text, "meta": meta}
    except OSError:
        return None


def _render_workspace(meta, body):
    ordered = ("workspace", "display_name", "root", "updated", "author", "session")
    lines = ["---"]
    for key in ordered:
        if meta.get(key):
            lines.append(f"{key}: {meta[key]}")
    for key in sorted(set(meta) - set(ordered)):
        if meta.get(key):
            lines.append(f"{key}: {meta[key]}")
    return "\n".join(lines) + "\n---\n\n" + (body or "").strip() + "\n"


def _legacy_candidates(vault):
    candidates = {}
    for path in _raw_candidates(vault):
        meta = _read_raw_meta(path)
        if meta.get("source") not in _SESSION_SOURCES:
            continue
        cwd = meta.get("cwd")
        old = legacy_workspace_key(cwd)
        key, root, display = workspace_key_detail(cwd)
        if old and key and root and display:
            candidates.setdefault(old, {})[key] = (root, display)
    return candidates


def migrate_legacy_workspace(vault, *, cwd=None):
    """Migrate old basename handoffs when their new key is unambiguous.

    Raw captures remain immutable. Ambiguous or conflicting pages are left in
    place and returned in ``skipped`` for a human decision.
    """
    vault = Path(vault)
    directory = vault / "workspace"
    result = {"migrated": [], "skipped": []}
    if not directory.is_dir():
        return result
    if cwd:
        old = legacy_workspace_key(cwd)
        key, root, display = workspace_key_detail(cwd)
        candidates = {old: ({key: (root, display)} if old and key and root and display else {})}
    else:
        candidates = _legacy_candidates(vault)
    for source in sorted(directory.glob("*.md")):
        meta, body = _split_frontmatter(source.read_text(encoding="utf-8", errors="ignore"))
        if meta.get("root") and meta.get("display_name"):
            continue
        old = meta.get("workspace") or source.stem
        options = candidates.get(old, {})
        if len(options) != 1:
            if options:
                result["skipped"].append(source.name)
            continue
        key, (root, display) = next(iter(options.items()))
        target = workspace_path(vault, key)
        if target.exists() and target != source:
            result["skipped"].append(source.name)
            continue
        meta.update({"workspace": key, "root": root, "display_name": display})
        target.write_text(_render_workspace(meta, body), encoding="utf-8")
        if target != source:
            source.unlink()
        result["migrated"].append(f"{source.name} -> {target.name}")
        vault_mod.log_append(vault, f"workspace migration: {source.stem} -> {key}")
    return result


def read_workspace(vault, workspace, *, cwd=None):
    if cwd:
        migrate_legacy_workspace(vault, cwd=cwd)
    path = workspace_path(vault, workspace)
    if not path.is_file():
        return None, None
    try:
        return _split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None, None


def write_workspace(vault, workspace, body, *, author, session_id=None, root=None, display_name=None):
    vault = Path(vault)
    existing, _ = read_workspace(vault, workspace)
    meta = {
        "workspace": workspace,
        "display_name": display_name or (existing or {}).get("display_name") or workspace,
        "root": root or (existing or {}).get("root") or "",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author": author,
        "session": session_id or "",
    }
    path = workspace_path(vault, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_workspace(meta, body), encoding="utf-8")
    vault_mod.log_append(vault, f"workspace/{workspace} updated ({author})")
    return path


def generate(vault, workspace, raw_text, *, current=""):
    lim = limits_mod.load(vault)
    vcfg = config_mod.load_vault(Path(vault))
    models = config_mod.resolve_models(vault_cfg=vcfg)
    model = models.get("propose") or models.get("merge")
    if not model:
        raise providers.ProviderError("no model configured for workspace page")
    body = providers.complete(
        WORKSPACE_PROMPT.format(
            workspace=workspace,
            current=(current or "")[-lim["workspace_max_chars"]:],
            raw=(raw_text or "")[-lim["workspace_source_chars"]:]),
        model=model)
    body = _sanitize_body(body, lim["workspace_max_chars"])
    if not body:
        raise providers.ProviderError("empty workspace-page body from provider")
    return body


def checkpoint_path(vault, workspace):
    safe = _kebab(workspace) or "workspace"
    return Path(vault) / ".memex" / "state" / "workspaces" / f"{safe}.json"


def extract_delta_views(text):
    """Split a cleaned transcript delta into cheap deterministic views.

    This keeps the incremental path LLM-agnostic: the model receives the
    conversation text, while callers can inspect tool/file and reference views
    without reparsing the full raw capture.
    """
    import re as _re
    text = text or ""
    tools = [line for line in text.splitlines()
             if _re.search(r"(?:_ran:|_edited:|_wrote:|_read:|_searched:|_tool:)", line)]
    refs = sorted(set(_re.findall(r"https?://[^\s)]+|\b[A-Z]{2,6}-\d{2,6}\b", text)))
    return {"text": text, "tools": "\n".join(tools), "references": refs}


def incremental_views(vault, workspace, raw_path, *, session_id=None):
    data = incremental_source(vault, workspace, raw_path, session_id=session_id)
    data["views"] = extract_delta_views(data["delta"])
    return data


def refresh_incremental_views(vault, workspace, raw_path, *, session_id=None,
                               root=None, display_name=None, provider=None):
    return refresh_incremental(vault, workspace, raw_path, session_id=session_id,
                               root=root, display_name=display_name, provider=provider)

def _read_checkpoint(vault, workspace):
    import json
    path = checkpoint_path(vault, workspace)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _write_checkpoint(vault, workspace, data):
    import json
    path = checkpoint_path(vault, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def incremental_source(vault, workspace, raw_path, *, session_id=None):
    """Return only the append-only transcript delta and its new checkpoint.

    Raw markdown is immutable after capture, so this uses a content hash of the
    processed prefix. If a session changes its prefix or the path/session changes,
    it safely falls back to the full current raw body.
    """
    import hashlib
    path = Path(raw_path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    meta, body = _split_frontmatter(text)
    checkpoint = _read_checkpoint(vault, workspace)
    previous_path = checkpoint.get("raw_path")
    previous_session = checkpoint.get("session_id")
    offset = int(checkpoint.get("processed_chars") or 0)
    prefix = body[:offset]
    prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
    compatible = (previous_path == str(path) and previous_session == session_id
                  and checkpoint.get("prefix_hash") == prefix_hash)
    delta = body[offset:] if compatible else body
    return {
        "meta": meta,
        "body": body,
        "delta": delta,
        "checkpoint": {
            "session_id": session_id,
            "raw_path": str(path),
            "processed_chars": len(body),
            "prefix_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        },
        "incremental": compatible,
    }


def refresh_incremental(vault, workspace, raw_path, *, session_id=None,
                         root=None, display_name=None):
    """Update a workspace from only new raw content, then advance its cursor."""
    data = incremental_source(vault, workspace, raw_path, session_id=session_id)
    _meta, current = read_workspace(vault, workspace)
    if not data["delta"].strip() and current:
        _write_checkpoint(vault, workspace, data["checkpoint"])
        return workspace_path(vault, workspace), data["incremental"], 0
    body = generate(vault, workspace, data["delta"], current=current or "")
    vcfg = config_mod.load_vault(Path(vault))
    models = config_mod.resolve_models(vault_cfg=vcfg)
    verify_model = config_mod.resolve_verify_model(vcfg, default=models.get("merge"))
    if verify_model:
        # The propose model that wrote `body` may be a small/free model filling
        # gaps in a thin slice with plausible-sounding fabrication (seen in
        # production: a near-empty raw slice synthesized into an elaborate,
        # entirely invented "current state"). This page is injected as trusted
        # context into every future session, so it gets the same fidelity gate
        # the wiki's delta/chunk merge already has — not writing it silently
        # unverified.
        verdict = verify_mod.verify_delta(data["delta"], current or "", body, model=verify_model)
        if not verdict.get("error") and verdict.get("outcome") in (ctr.Outcome.UNSUPPORTED, ctr.Outcome.CONFLICTING):
            vault_mod.log_append(
                vault, f"workspace/{workspace} refresh rejected (unfaithful: {verdict.get('reason', '')[:160]})")
            _write_checkpoint(vault, workspace, data["checkpoint"])
            return workspace_path(vault, workspace), data["incremental"], 0
    path = write_workspace(vault, workspace, body, author="auto", session_id=session_id,
                           root=root, display_name=display_name)
    _write_checkpoint(vault, workspace, data["checkpoint"])
    return path, data["incremental"], len(data["delta"])


def read_checkpoint(vault, workspace):
    return _read_checkpoint(vault, workspace)


def write_checkpoint(vault, workspace, data):
    return _write_checkpoint(vault, workspace, data)


def mark_checkpoint(vault, workspace, raw_path, *, session_id=None):
    """Advance a checkpoint without an LLM call (used after a no-op delta)."""
    data = incremental_source(vault, workspace, raw_path, session_id=session_id)
    _write_checkpoint(vault, workspace, data["checkpoint"])
    return data


def _sanitize_body(body, max_chars):
    body = (body or "").strip()
    body = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", body).strip()
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:].lstrip("\n")
    start = body.find("## ")
    if start > 0:
        body = body[start:]
    return body[:max_chars].strip()
