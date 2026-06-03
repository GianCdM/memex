"""Codex CLI session backend.

On-disk format (confirmed on this machine):
    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Each line is `{type, timestamp, payload}`. Relevant lines:
  - "session_meta" (first line): payload.id (session UUID), payload.cwd,
    payload.timestamp.
  - "event_msg" payload.type == "user_message": payload.message is the clean
    user input (USER turns). It may be prefixed with an injected
    "# Context from my IDE setup:" block — stripped.
  - "response_item" payload.type == "message", role == "assistant":
    payload.content is a list of {type, text} parts -> ASSISTANT turns.
  - "response_item"/"event_msg" tool activity (function_call, custom_tool_call,
    exec_command, patch_apply) -> condensed to one-line actions.
  - role == "developer"/system instructions and reasoning -> dropped. A
    role == "user" response_item wrapping injected <...> context is only used as
    a fallback when no user_message events exist.

Titles + dates also come from ~/.codex/session_index.jsonl
({id, thread_name, updated_at}), keyed by the session UUID.

Stdlib only; never raises on a malformed session.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / ".codex" / "sessions"
INDEX = Path.home() / ".codex" / "session_index.jsonl"

_DATA_URI_RE = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{200,}={0,2}\b")
# Codex sometimes injects IDE/environment context at the head of a user message.
_IDE_CTX_RE = re.compile(r"^#\s*Context from my IDE setup:.*?(?=\n\S|\Z)", re.DOTALL)


def available() -> bool:
    """True if Codex CLI's local session storage exists on the machine."""
    return ROOT.is_dir()


def iter_sessions(workspace=None, since=None):
    """Yield one cleaned session dict per Codex rollout file."""
    if not available():
        return
    since_dt = _parse_since(since)
    ws = os.path.abspath(workspace) if workspace else None
    idx = _read_index()

    for fp in sorted(ROOT.glob("**/*.jsonl")):
        try:
            sess = _read_session(fp, idx)
        except Exception:
            continue
        if sess is None:
            continue
        if ws is not None and not _within(sess["cwd"], ws):
            continue
        if since_dt is not None and not _on_or_after(sess["date"], since_dt):
            continue
        yield sess


# --------------------------------------------------------------------------- #
# Index (titles + updated timestamps)
# --------------------------------------------------------------------------- #
def _read_index():
    idx = {}
    if not INDEX.is_file():
        return idx
    for d in _iter_jsonl(INDEX):
        sid = d.get("id")
        if sid:
            idx[sid] = (d.get("thread_name"), d.get("updated_at"))
    return idx


# --------------------------------------------------------------------------- #
# Parsing + cleaning
# --------------------------------------------------------------------------- #
def _iter_jsonl(fp: Path):
    try:
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _read_session(fp: Path, idx):
    sid = None
    cwd = None
    meta_ts = None
    turns = []          # list of (role, text)
    fallback_user = []  # role==user response_items, used only if no user_message

    for d in _iter_jsonl(fp):
        t = d.get("type")
        pl = d.get("payload")
        if not isinstance(pl, dict):
            continue

        if t == "session_meta":
            sid = pl.get("id") or sid
            cwd = pl.get("cwd") or cwd
            meta_ts = pl.get("timestamp") or d.get("timestamp") or meta_ts

        elif t == "event_msg":
            pt = pl.get("type")
            if pt == "user_message":
                msg = pl.get("message")
                if isinstance(msg, str):
                    cleaned = _clean_text(_strip_ide_ctx(msg))
                    if cleaned:
                        turns.append(("user", cleaned))

        elif t == "response_item":
            pt = pl.get("type")
            if pt == "message":
                role = pl.get("role")
                if role == "assistant":
                    txt = _clean_text(_parts_text(pl.get("content")))
                    if txt:
                        turns.append(("assistant", txt))
                elif role == "user":
                    raw = _parts_text(pl.get("content")).strip()
                    # skip injected <environment_context>/<...> wrappers
                    if raw and not raw.lstrip().startswith("<"):
                        fallback_user.append(_clean_text(_strip_ide_ctx(raw)))
            elif pt in ("function_call", "custom_tool_call"):
                line = _condense_tool_call(pl)
                if line:
                    turns.append(("assistant", line))

    if not any(r == "user" for r, _ in turns) and fallback_user:
        turns = [("user", u) for u in fallback_user if u] + turns

    if not sid or not turns:
        return None

    title, updated = idx.get(sid, (None, None))
    if not title:
        first_user = next((tx for r, tx in turns if r == "user"), "")
        title = first_user.splitlines()[0] if first_user else ""
    title = (title or "").strip()[:120] or "(sem título)"

    return {
        "source": "codex",
        "id": sid,
        "title": title,
        "date": _iso(meta_ts or updated),
        "cwd": cwd,
        "text": _to_markdown(turns),
    }


def _parts_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return ""


def _condense_tool_call(pl) -> str:
    """One human-readable line for a Codex tool/command call."""
    name = (pl.get("name") or "").strip()
    raw_args = pl.get("arguments")
    args = {}
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    elif isinstance(raw_args, dict):
        args = raw_args

    # shell / exec commands
    if name in ("shell", "exec", "local_shell", "container.exec") or "command" in args:
        cmd = args.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        cmd = (cmd or "").strip().splitlines()
        cmd = cmd[0] if cmd else ""
        # Codex wraps commands as ['bash','-lc','<cmd>']; surface the payload.
        cmd = re.sub(r"^(bash|sh|zsh)\s+-l?c\s+", "", cmd).strip("'\" ")
        return f"_ran: {_oneline(cmd, 120)}_" if cmd else "_ran a command_"
    if name in ("apply_patch", "patch"):
        return "_edited files (patch)_"
    short = name.split("__")[-1] if name else "tool"
    hint = ""
    for k in ("query", "path", "file_path", "input", "prompt"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            hint = _oneline(v, 80)
            break
    return f"_tool: {short}{(' — ' + hint) if hint else ''}_"


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


def _strip_ide_ctx(text: str) -> str:
    return _IDE_CTX_RE.sub("", text or "").strip()


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = _DATA_URI_RE.sub("[data]", text)
    text = _LONG_B64_RE.sub("[base64]", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _oneline(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


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
