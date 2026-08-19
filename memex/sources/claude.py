"""Claude Code session backend.

On-disk format (confirmed on this machine):
    ~/.claude/projects/<cwd-encoded>/<id>.jsonl

Each line is a JSON object with a `type`. Relevant types:
  - "user" / "assistant": message in `message.content` (string OR list of
    blocks: text / thinking / tool_use / tool_result).
  - "ai-title": auto-generated title in `message.aiTitle`.
  - everything else (last-prompt, system, attachment, queue-operation, ...) is
    ignored.

cwd encoding: both '/' and '.' are replaced with '-'
(/Users/ana/dev/my.app -> -Users-ana-dev-my-app). We also read the `cwd` field
that Claude records on each line, which is the reliable absolute path used for
the workspace filter (the directory name alone is lossy).

Output `text` is alternating '## user' / '## assistant' blocks: thinking, tool
payload noise, system reminders and base64 are stripped; tool calls are
condensed to one line (e.g. 'ran: pytest', 'edited: foo.py').

Stdlib only; never raises on a malformed session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
# Validate an append boundary without re-reading a giant transcript prefix on
# every compact. The source JSONL is append-only; inode/device plus this bounded
# trailing signature catches rotation, truncation and normal rewrite accidents.
_CAPTURE_BOUNDARY_BYTES = 4096

# Inline data / very long opaque tokens (base64, data: URIs) -> drop.
_DATA_URI_RE = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{200,}={0,2}\b")
# Injected XML-ish noise that wraps the human turn (reminders, slash commands).
_NOISE_TAG_RE = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|command-contents"
    r"|local-command-stdout|local-command-stderr|local-command-caveat)>"
    r".*?</\1>",
    re.DOTALL,
)
_SELFCLOSING_NOISE_RE = re.compile(r"<(command-name|command-message|command-args)\s*/>")


def available() -> bool:
    """True if Claude Code's local storage exists on the machine."""
    return PROJECTS_ROOT.is_dir()


def read_transcript(path):
    """Parse ONE transcript file (a hook's `transcript_path`) into a session
    dict — same shape as iter_sessions yields. None on any problem (a hook
    caller must never crash on a malformed/missing transcript)."""
    try:
        fp = Path(path).expanduser()
        if not fp.is_file():
            return None
        return _read_session(fp)
    except Exception:
        return None


def transcript_fingerprint(path) -> str:
    """Stable, non-secret identifier for a local transcript path.

    Capture state must distinguish two transcript files with the same Claude
    session id (for example after a harness migration), but must not persist
    the potentially sensitive absolute path itself.
    """
    try:
        value = str(Path(path).expanduser().resolve())
    except Exception:
        value = str(path or "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_transcript_delta(path, cursor=None):
    """Read only the complete JSONL lines appended after ``cursor``.

    The cursor is byte-based so UTF-8 multi-byte characters cannot corrupt the
    boundary. Its prefix hash proves that the file has not been rewritten under
    us. The returned dict has a ``status`` of ``initial``, ``delta``,
    ``unchanged`` or ``incompatible`` and never raises: hooks must stay silent
    and fast even while Claude is still appending the transcript.
    """
    cursor = cursor or {}
    try:
        fp = Path(path).expanduser()
        if not fp.is_file():
            return {"status": "incompatible", "reason": "missing-transcript"}
        stat = fp.stat()
        size = stat.st_size
        offset = int(cursor.get("transcript_to") or 0)
        path_hash = transcript_fingerprint(fp)
        if cursor and cursor.get("transcript_path_hash") != path_hash:
            return {"status": "incompatible", "reason": "transcript-path-changed"}
        if cursor.get("transcript_device") not in (None, stat.st_dev):
            return {"status": "incompatible", "reason": "transcript-device-changed"}
        if cursor.get("transcript_inode") not in (None, getattr(stat, "st_ino", 0)):
            return {"status": "incompatible", "reason": "transcript-inode-changed"}
        if offset < 0 or offset > size:
            return {"status": "incompatible", "reason": "transcript-truncated"}
        if offset:
            # The old cursor schema stored a whole-prefix hash. Accept it once
            # for migration, then write the O(1)-sized boundary signature below.
            if cursor.get("transcript_boundary_sha256"):
                with fp.open("rb") as handle:
                    handle.seek(max(0, offset - _CAPTURE_BOUNDARY_BYTES))
                    boundary = handle.read(offset - max(0, offset - _CAPTURE_BOUNDARY_BYTES))
                if hashlib.sha256(boundary).hexdigest() != cursor.get("transcript_boundary_sha256"):
                    return {"status": "incompatible", "reason": "transcript-prefix-changed"}
            elif cursor.get("transcript_prefix_sha256"):
                with fp.open("rb") as handle:
                    prefix = handle.read(offset)
                if hashlib.sha256(prefix).hexdigest() != cursor.get("transcript_prefix_sha256"):
                    return {"status": "incompatible", "reason": "transcript-prefix-changed"}
        if offset == size:
            return {"status": "unchanged", "next_cursor": dict(cursor)}

        with fp.open("rb") as handle:
            handle.seek(offset)
            appended = handle.read()
        # A hook can race a JSONL write. Process through the last complete
        # newline, except for a final line that already parses as a complete
        # JSON object (test fixtures and normal JSONL writers often omit the
        # terminal newline). An incomplete tail is retried next time.
        end = appended.rfind(b"\n")
        if end < len(appended) - 1:
            tail = appended[end + 1:]
            try:
                json.loads(tail)
            except (UnicodeDecodeError, json.JSONDecodeError):
                complete = appended[:end + 1] if end >= 0 else b""
            else:
                complete = appended
        else:
            complete = appended
        if not complete:
            return {"status": "unchanged", "next_cursor": dict(cursor)}
        new_offset = offset + len(complete)
        entries = []
        for raw in complete.splitlines():
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                entries.append(data)

        session = _session_from_entries(
            entries,
            sid=fp.stem,
            previous_role=cursor.get("last_role"),
            fallback_date=cursor.get("first_timestamp"),
            fallback_cwd=cursor.get("cwd"),
            fallback_title=cursor.get("title"),
        )
        with fp.open("rb") as handle:
            boundary_from = max(0, new_offset - _CAPTURE_BOUNDARY_BYTES)
            handle.seek(boundary_from)
            boundary = handle.read(new_offset - boundary_from)
        next_cursor = {
            "version": 2,
            "source": "claude",
            "session_id": fp.stem,
            "transcript_path_hash": path_hash,
            "transcript_to": new_offset,
            "transcript_boundary_sha256": hashlib.sha256(boundary).hexdigest(),
            "transcript_device": stat.st_dev,
            "transcript_inode": getattr(stat, "st_ino", 0),
            "last_role": session.get("last_role") if session else cursor.get("last_role"),
            "first_timestamp": (session or {}).get("date") or cursor.get("first_timestamp"),
            "cwd": (session or {}).get("cwd") or cursor.get("cwd"),
            "title": (session or {}).get("title") or cursor.get("title"),
        }
        if not session:
            return {"status": "unchanged", "next_cursor": next_cursor}
        session.pop("last_role", None)
        return {"status": "initial" if not cursor else "delta", "session": session,
                "next_cursor": next_cursor, "from_byte": offset, "to_byte": new_offset,
                "path_hash": path_hash}
    except Exception:
        return {"status": "incompatible", "reason": "read-failed"}


def iter_sessions(workspace=None, since=None):
    """Yield one cleaned session dict per Claude Code transcript."""
    if not available():
        return
    since_dt = _parse_since(since)
    ws = os.path.abspath(workspace) if workspace else None

    for fp in _session_files(ws):
        try:
            sess = _read_session(fp)
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
# File discovery
# --------------------------------------------------------------------------- #
def _encode_cwd(cwd: str) -> str:
    """Claude Code's project-dir encoding: every non-alphanumeric char -> '-'.
    Portable: /Users/ana/dev/my.app -> -Users-ana-dev-my-app, and on Windows
    C:\\src\\memex -> C--src-memex (v1 only mapped '/' and '.', so the prefix
    scan never matched a Windows workspace and sessions were never captured)."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _session_files(ws):
    """All candidate *.jsonl files.

    When a workspace is given we still scan a superset (the encoded dir name is
    lossy — '.' and '/' both map to '-'), then filter precisely on each
    session's recorded `cwd`. The encoded-name prefix narrows the scan.
    """
    if not PROJECTS_ROOT.is_dir():
        return
    if ws:
        enc = _encode_cwd(ws)
        for pdir in PROJECTS_ROOT.iterdir():
            if not pdir.is_dir():
                continue
            # subdir of ws -> encoded(ws) is a prefix of encoded(subdir)
            if pdir.name == enc or pdir.name.startswith(enc + "-"):
                yield from sorted(pdir.glob("*.jsonl"))
    else:
        for pdir in PROJECTS_ROOT.iterdir():
            if pdir.is_dir():
                yield from sorted(pdir.glob("*.jsonl"))


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


def _read_session(fp: Path):
    """Parse one transcript into a session dict (or None if it has no turns)."""
    return _session_from_entries(_iter_jsonl(fp), sid=fp.stem)


def _session_from_entries(entries, *, sid, previous_role=None, fallback_date=None,
                          fallback_cwd=None, fallback_title=None):
    """Render Claude JSONL entries to the public session shape.

    ``previous_role`` lets an incremental window continue a run of same-role
    turns without pretending the window started a new conversational block.
    """
    ai_title = fallback_title
    first_prompt = None
    first_ts = fallback_date
    cwd = fallback_cwd
    turns = []
    last_role = previous_role

    for d in entries:
        t = d.get("type")
        ts = d.get("timestamp")
        if ts and first_ts is None:
            first_ts = ts
        if cwd is None and isinstance(d.get("cwd"), str):
            cwd = d["cwd"]

        if t == "ai-title":
            msg = d.get("message")
            if isinstance(msg, dict):
                ai_title = msg.get("aiTitle") or ai_title
            ai_title = ai_title or d.get("aiTitle")
            continue
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _render_blocks(content)
        if not text:
            continue
        role = "user" if t == "user" else "assistant"
        if role == "user" and first_prompt is None:
            first_prompt = text
        turns.append((role, text))
        last_role = role

    if not turns:
        return None
    title = (ai_title or first_prompt or "").strip().splitlines()[0] if (ai_title or first_prompt) else ""
    title = title[:120] or "(sem título)"
    return {
        "source": "claude", "id": sid, "title": title,
        "date": _iso(first_ts), "cwd": cwd,
        "text": _to_markdown(turns, previous_role=previous_role),
        "last_role": last_role,
    }


def _render_blocks(content) -> str:
    """Turn a message's content into clean text, condensing tool calls."""
    if isinstance(content, str):
        return _clean_text(content)
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            parts.append(_clean_text(b.get("text") or ""))
        elif bt == "tool_use":
            line = _condense_tool_use(b.get("name") or "", b.get("input"))
            if line:
                parts.append(line)
        # thinking / tool_result / image / others -> dropped
    return "\n\n".join(p for p in parts if p).strip()


def _condense_tool_use(name: str, inp) -> str:
    """One human-readable line per tool call (no payload, no output)."""
    inp = inp if isinstance(inp, dict) else {}
    short = name.split("__")[-1] if name else "tool"

    if name == "Bash":
        cmd = (inp.get("command") or "").strip().splitlines()
        cmd = cmd[0] if cmd else ""
        return f"_ran: {_oneline(cmd, 120)}_" if cmd else "_ran a command_"
    if name in ("Edit", "MultiEdit"):
        return f"_edited: {_basename(inp.get('file_path'))}_"
    if name == "Write":
        return f"_wrote: {_basename(inp.get('file_path'))}_"
    if name == "Read":
        return f"_read: {_basename(inp.get('file_path'))}_"
    if name in ("Grep", "Glob"):
        q = inp.get("pattern") or inp.get("query") or ""
        return f"_searched: {_oneline(q, 80)}_" if q else f"_{short}_"
    if name in ("TodoWrite",):
        return "_updated todo list_"
    if name in ("Task",):
        desc = inp.get("description") or inp.get("subagent_type") or ""
        return f"_ran agent: {_oneline(desc, 80)}_" if desc else "_ran agent_"
    if name == "WebFetch":
        return f"_fetched: {_oneline(inp.get('url') or '', 100)}_"
    if name in ("WebSearch",):
        return f"_web search: {_oneline(inp.get('query') or '', 80)}_"
    # MCP / other tools: name + a short hint from a likely free-text field.
    hint = ""
    for k in ("query", "description", "prompt", "path", "url", "file_path"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            hint = _oneline(v, 80)
            break
    return f"_tool: {short}{(' — ' + hint) if hint else ''}_"


# --------------------------------------------------------------------------- #
# Markdown + text hygiene
# --------------------------------------------------------------------------- #
def _to_markdown(turns, *, previous_role=None) -> str:
    """Render turns as alternating '## user' / '## assistant' blocks.

    Consecutive turns of the SAME role are collapsed into one block (a session
    emits one assistant message per tool call, so a long run otherwise produces
    thousands of single-line '## assistant' headers). ``previous_role`` is
    metadata from an earlier incremental raw: a new raw is autonomous evidence,
    so it still opens its own heading even when it continues that role.
    """
    out = []
    for role, text in turns:
        text = (text or "").strip()
        if not text:
            continue
        if out and out[-1].startswith(f"## {role}\n"):
            out[-1] += "\n\n" + text
        else:
            out.append(f"## {role}\n\n{text}")
    return "\n\n".join(out)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = _NOISE_TAG_RE.sub("", text)
    text = _SELFCLOSING_NOISE_RE.sub("", text)
    text = _DATA_URI_RE.sub("[data]", text)
    text = _LONG_B64_RE.sub("[base64]", text)
    # collapse runs of blank lines left by stripping
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _oneline(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _basename(path) -> str:
    if not path:
        return "?"
    return os.path.basename(str(path)) or str(path)


# --------------------------------------------------------------------------- #
# Workspace + date filtering
# --------------------------------------------------------------------------- #
def _within(cwd, ws) -> bool:
    """True if `cwd` == ws or a subdirectory of ws (case/sep-insensitive on
    Windows — v1 compared with '/' only, so the workspace filter dropped
    every session on Windows)."""
    if not cwd:
        return False
    try:
        c = os.path.normcase(os.path.abspath(cwd))
        w = os.path.normcase(os.path.abspath(ws))
    except Exception:
        return False
    return c == w or c.startswith(w.rstrip("\\/") + os.sep)


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
        return True  # unknown date -> don't drop
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
    """Normalize a timestamp (ISO string or epoch s/ms) to ISO8601, best effort."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    try:
        v = float(val)
        if v > 1e12:  # epoch milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None
