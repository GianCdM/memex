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

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

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
    return cwd.replace("/", "-").replace(".", "-")


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
    ai_title = None
    first_prompt = None
    first_ts = None
    cwd = None
    turns = []  # list of (role, text)

    for d in _iter_jsonl(fp):
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

    if not turns:
        return None

    title = (ai_title or first_prompt or "").strip().splitlines()[0] if (ai_title or first_prompt) else ""
    title = title[:120] or "(sem título)"

    return {
        "source": "claude",
        "id": fp.stem,
        "title": title,
        "date": _iso(first_ts),
        "cwd": cwd,
        "text": _to_markdown(turns),
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
    """True if `cwd` == ws or a subdirectory of ws."""
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
