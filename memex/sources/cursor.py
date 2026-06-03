"""Cursor session backend.

On-disk format (confirmed on this machine):
  - Conversation content lives in the GLOBAL db:
        ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
    table `cursorDiskKV`:
      * composerData:<id>            -> thread metadata JSON (createdAt ms, text)
      * bubbleId:<composerId>:<id>   -> one message; `type` 1 = user, 2 = AI,
                                        content in `text` (markdown).
  - Workspace link (for the `workspace` filter — composers carry no cwd):
        ~/Library/.../Cursor/User/workspaceStorage/<hash>/workspace.json
            -> "folder": "file:///abs/path"
        ~/Library/.../Cursor/User/workspaceStorage/<hash>/state.vscdb
            ItemTable key 'composer.composerData' -> allComposers[].composerId
    so composer -> cwd is resolved by which workspace lists that composer id.

Open read-only (`?mode=ro`) so it works even with Cursor running. Bubbles are
ordered by their `createdAt`. Output `text` is alternating '## user' /
'## assistant'; base64 stripped; agentic tool activity recorded on a bubble is
condensed to a one-line action.

Stdlib only; never raises on a malformed session.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

_DATA_URI_RE = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{200,}={0,2}\b")


def _base_dir() -> Path:
    import sys

    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Cursor/User"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", "")) / "Cursor/User"
    return Path.home() / ".config/Cursor/User"


def _global_db() -> Path:
    return _base_dir() / "globalStorage" / "state.vscdb"


def available() -> bool:
    """True if Cursor's global session db exists on the machine."""
    return _global_db().is_file()


def iter_sessions(workspace=None, since=None):
    """Yield one cleaned session dict per Cursor composer thread."""
    if not available():
        return
    since_dt = _parse_since(since)
    ws = os.path.abspath(workspace) if workspace else None

    try:
        meta, convos = _load_global()
    except Exception:
        return
    cwd_by_composer = _composer_cwd_map()

    for cid, turns in convos.items():
        try:
            turns = sorted(turns, key=lambda x: _sortkey(x[2]))
            cwd = cwd_by_composer.get(cid)
            if ws is not None and not _within(cwd, ws):
                continue
            m = meta.get(cid, {})
            date = _iso(m.get("created"))
            if since_dt is not None and not _on_or_after(date, since_dt):
                continue
            first_user = next((tx for r, tx, _ in turns if r == "user"), "")
            title = (m.get("text") or first_user or "").strip().splitlines()
            title = (title[0] if title else "")[:120] or "(sem título)"
            md = _to_markdown([(r, tx) for r, tx, _ in turns])
            if not md:
                continue
            yield {
                "source": "cursor",
                "id": cid,
                "title": title,
                "date": date,
                "cwd": cwd,
                "text": md,
            }
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# Global db: composer metadata + bubbles
# --------------------------------------------------------------------------- #
def _connect_ro(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _load_global():
    """Return ({composerId: {created, text}}, {composerId: [(role, text, created)]})."""
    con = _connect_ro(_global_db())
    meta = {}
    convos = {}
    try:
        for key, value in con.execute(
            "SELECT key,value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            cid = key.split(":", 1)[1]
            d = _loads(value)
            if d is None:
                continue
            meta[cid] = {
                "created": d.get("createdAt"),
                "text": (d.get("text") or "").strip(),
            }
        for key, value in con.execute(
            "SELECT key,value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ):
            parts = key.split(":")
            if len(parts) < 3:
                continue
            cid = parts[1]
            b = _loads(value)
            if b is None:
                continue
            role = "user" if b.get("type") == 1 else "assistant"
            text = _bubble_text(b, role)
            if not text:
                continue
            convos.setdefault(cid, []).append((role, text, b.get("createdAt") or 0))
    finally:
        con.close()
    return meta, convos


def _bubble_text(b, role) -> str:
    """Clean visible text for a bubble; condense agentic tool activity."""
    text = _clean_text((b.get("text") or "").strip())
    if text:
        return text
    if role != "assistant":
        return ""
    # No prose: surface a condensed action from common agentic-bubble fields.
    actions = []
    diffs = b.get("assistantSuggestedDiffs") or b.get("gitDiffs") or []
    if isinstance(diffs, list):
        for d in diffs:
            if isinstance(d, dict):
                fp = d.get("uri") or d.get("filePath") or d.get("path")
                if fp:
                    actions.append(f"edited: {_basename(fp)}")
    tr = b.get("toolResults") or b.get("interpreterResults") or []
    if isinstance(tr, list) and tr:
        actions.append("ran a tool")
    actions = list(dict.fromkeys(actions))[:5]
    return "\n".join(f"_{a}_" for a in actions)


# --------------------------------------------------------------------------- #
# Workspace link: composerId -> cwd
# --------------------------------------------------------------------------- #
def _composer_cwd_map():
    """Map composerId -> absolute folder path, via per-workspace state dbs."""
    mapping = {}
    ws_root = _base_dir() / "workspaceStorage"
    if not ws_root.is_dir():
        return mapping
    for wdir in ws_root.iterdir():
        if not wdir.is_dir():
            continue
        folder = _workspace_folder(wdir / "workspace.json")
        if not folder:
            continue
        db = wdir / "state.vscdb"
        if not db.is_file():
            continue
        for cid in _workspace_composer_ids(db):
            mapping.setdefault(cid, folder)
    return mapping


def _workspace_folder(wsjson: Path):
    try:
        d = json.loads(wsjson.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    folder = d.get("folder")
    if not isinstance(folder, str):
        return None
    if folder.startswith("file://"):
        return unquote(urlparse(folder).path) or None
    return folder or None


def _workspace_composer_ids(db: Path):
    ids = []
    try:
        con = _connect_ro(db)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='composer.composerData'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ids
    if not row:
        return ids
    d = _loads(row[0])
    if not isinstance(d, dict):
        return ids
    for c in d.get("allComposers") or []:
        if isinstance(c, dict) and c.get("composerId"):
            ids.append(c["composerId"])
    return ids


# --------------------------------------------------------------------------- #
# Markdown + text hygiene
# --------------------------------------------------------------------------- #
def _to_markdown(turns) -> str:
    out = []
    for role, text in turns:
        text = (text or "").strip()
        if not text:
            continue
        out.append(f"## {role}\n\n{text}")
    return "\n\n".join(out)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = _DATA_URI_RE.sub("[data]", text)
    text = _LONG_B64_RE.sub("[base64]", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _basename(path) -> str:
    if not path:
        return "?"
    s = str(path)
    if s.startswith("file://"):
        s = unquote(urlparse(s).path)
    return os.path.basename(s) or s


def _loads(value):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Workspace + date filtering
# --------------------------------------------------------------------------- #
def _within(cwd, ws) -> bool:
    if not cwd:
        return False
    try:
        c = os.path.abspath(cwd)
    except Exception:
        return False
    return c == ws or c.startswith(ws.rstrip("/") + "/")


def _parse_since(since):
    if not since:
        return None
    try:
        return datetime.strptime(since[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _on_or_after(date_str, since_date) -> bool:
    d = _date_only(date_str)
    if d is None:
        return True
    return d >= since_date


def _date_only(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


def _sortkey(created):
    """Bubbles store createdAt as epoch ms (int) or ISO string; sort uniformly."""
    if isinstance(created, (int, float)):
        return (0, float(created))
    if isinstance(created, str):
        try:
            return (0, datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return (1, created)
    return (2, 0.0)


def _iso(val):
    if val is None:
        return None
    if isinstance(val, str):
        return val
    try:
        v = float(val)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None
