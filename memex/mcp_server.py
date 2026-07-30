"""memex MCP server — expose the brain as tools for AI agents.

Stdlib-only JSON-RPC 2.0 over stdio (Content-Length framed). No external
dependencies — the MCP transport is simple enough to implement directly.

Tools exposed (what the agent calls mid-session):
  search   — find pages in the brain, returning structured results with paths
  remember — file one durable fact into the brain right now
  status   — peek at the brain: raw notes, wiki pages, pending

Start:  memex mcp   (or `python -m memex.mcp_server`)
"""

from __future__ import annotations

import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path

from . import config as config_mod
from . import ingest as ingest_mod
from . import limits as limits_mod
from . import workspace as workspace_mod
from . import recall as recall_mod
from . import synth as synth_mod
from . import vault as vault_mod

# ── MCP Protocol (JSON-RPC 2.0 over Content-Length-framed stdio) ───────────

SERVER_INFO = {
    "name": "memex",
    "version": "0.1.0",
}

TOOLS = [
    {
        "name": "search",
        "description": "Search the memex brain for pages matching a query. "
                       "Returns scored results with file paths — Read the path for full detail. "
                       "Use when the user asks about past decisions, people, teams, meetings, "
                       "or any knowledge that might be in their second brain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms (keywords, not natural language). "
                                   "Use the user's own words when possible.",
                },
                "vault": {
                    "type": "string",
                    "description": "Path to the vault. Resolves automatically if omitted.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default: 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "File one durable fact, decision, or preference into the memex brain. "
                       "The text is synthesized into a wiki page immediately. "
                       "Use when the user says 'remember this', 'save this', or shares "
                       "something worth keeping permanently.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "One clear, self-contained paragraph to save. "
                                   "Include rationale (why) and context.",
                },
                "vault": {
                    "type": "string",
                    "description": "Path to the vault. Resolves automatically if omitted.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "status",
        "description": "Peek at the memex brain — how many raw notes, wiki pages, "
                       "pending synthesis, workspace-pages, kinds, and statuses. "
                       "Use when the user asks about their brain's state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {
                    "type": "string",
                    "description": "Path to the vault. Resolves automatically if omitted.",
                },
            },
        },
    },
]


def _resolve_vault(vault_arg=None):
    try:
        return config_mod.resolve_vault(vault_arg)
    except Exception:
        return None


# ── Tool implementations ────────────────────────────────────────────────────

def _tool_search(query, vault=None, limit=5):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}

    try:
        index = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "the brain has no index yet"}

    lim = dict(limits_mod.load(vault))
    lim["retrieve_min_overlap"] = 1
    lim["retrieve_min_score"] = 0.0

    scored = recall_mod.hybrid_rank(
        index.get("pages", []), query, lim, vault,
        min_tokens=1, log_prefix="memex mcp",
    )

    results = []
    for score, p in scored[:max(1, int(limit))]:
        path = str(vault / "wiki" / (p.get("path") or ""))
        results.append({
            "title": p.get("title", ""),
            "slug": p.get("slug", ""),
            "score": round(score, 3),
            "tier": p.get("tier", "silver"),
            "section": p.get("section", ""),
            "summary": " ".join((p.get("summary") or "").split())[:220],
            "tags": p.get("tags", []),
            "path": path,
        })

    return {
        "ok": True,
        "query": query,
        "total": len(scored),
        "results": results,
    }


def _tool_remember(text, vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}

    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty text"}

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
        return {"ok": False, "error": "nothing saved (empty or already known)"}

    vault_mod.log_append(vault, f"remember: {text[:80]}")

    # Inline synthesis — try to compile into wiki immediately
    synth_mod.run(Namespace(
        vault=str(vault), provider=None, limit=None, since=None,
        only=fname, model_propose=None, model_merge=None,
    ))

    try:
        synthed = json.loads((vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
    except Exception:
        synthed = {}

    return {
        "ok": True,
        "file": f"raw/{fname}",
        "synthesized": fname in synthed,
    }


def _tool_status(vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}

    mx = vault / ".memex"
    raw = list((vault / "raw").glob("*.md"))
    try:
        pages = json.loads((mx / "index.json").read_text(encoding="utf-8")).get("pages", [])
    except Exception:
        pages = []
    try:
        synthed = json.loads((mx / "synthed.json").read_text(encoding="utf-8"))
    except Exception:
        synthed = {}

    kinds = {}
    statuses = {}
    for p in pages:
        k = p.get("kind", "session")
        kinds[k] = kinds.get(k, 0) + 1
        s = p.get("status", "current")
        statuses[s] = statuses.get(s, 0) + 1

    workspace_pages = sorted((vault / "workspace").glob("*.md")) if (vault / "workspace").is_dir() else []

    suggestions = 0
    sug = vault / "wiki" / "_sugestoes.md"
    if sug.exists():
        suggestions = sum(1 for ln in sug.read_text(encoding="utf-8").splitlines()
                         if ln.startswith("## "))

    return {
        "ok": True,
        "vault": str(vault),
        "raw_notes": len(raw),
        "synthesized": len(synthed),
        "pending": max(0, len(raw) - len(synthed)),
        "wiki_pages": len(pages),
        "kinds": kinds,
        "statuses": statuses,
        "workspace_pages": [p.stem for p in workspace_pages],
        "suggestions": suggestions,
    }


_TOOL_DISPATCH = {
    "search": _tool_search,
    "remember": _tool_remember,
    "status": _tool_status,
}


# ── JSON-RPC over stdio ─────────────────────────────────────────────────────

_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB — anything bigger is not a real request


def _log(msg: str) -> None:
    """Write a diagnostic line to stderr (never stdout — that's the protocol)."""
    print(f"[memex mcp] {msg}", file=sys.stderr, flush=True)


def _read_message() -> dict | None:
    """Read one Content-Length-framed JSON-RPC message from stdin.

    Returns None on EOF or unrecoverable framing error. The caller treats
    None as "client is done" and exits the loop cleanly. Every error path
    logs to stderr so silent failures become diagnosable.
    """
    try:
        # ── read headers until blank line ──────────────────────────────
        content_length = None
        header_lines: list[str] = []
        while True:
            line = sys.stdin.buffer.readline()
            if not line:  # EOF
                _log("stdin closed (EOF)")
                return None

            try:
                decoded = line.decode("ascii", errors="ignore").strip()
            except Exception:
                decoded = ""

            if not decoded:
                break  # blank line = end of headers

            header_lines.append(decoded)
            if decoded.lower().startswith("content-length:"):
                try:
                    content_length = int(decoded.split(":", 1)[1].strip())
                except ValueError:
                    pass

        if content_length is None:
            # Try newline-delimited JSON as a fallback (non-standard but
            # common in dev tools and manual testing).
            if header_lines:
                try:
                    return json.loads(header_lines[0])
                except json.JSONDecodeError:
                    pass
            _log(f"no Content-Length header in: {header_lines[:3]}")
            return None

        if content_length < 0 or content_length > _MAX_BODY_BYTES:
            _log(f"Content-Length {content_length} out of range — ignoring")
            return None

        # ── read body ──────────────────────────────────────────────────
        body_bytes = sys.stdin.buffer.read(content_length)
        if len(body_bytes) < content_length:
            _log(f"short read: expected {content_length} bytes, got {len(body_bytes)}")
            return None

        return json.loads(body_bytes)

    except json.JSONDecodeError as e:
        _log(f"invalid JSON body: {e}")
        return None
    except Exception as e:
        _log(f"read error: {type(e).__name__}: {e}")
        return None


def _write_message(data: dict) -> None:
    """Write one Content-Length-framed JSON-RPC message to stdout."""
    body = json.dumps(data, ensure_ascii=False)
    header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
    sys.stdout.buffer.write(header.encode("ascii"))
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.flush()


def _handle_request(msg: dict) -> dict | None:
    """Handle a single JSON-RPC request. Returns the response or None for notifications."""
    req_id = msg.get("id")
    method = msg.get("method", "")

    # Notifications — no response
    if req_id is None:
        if method == "notifications/initialized":
            return None
        return None

    # initialize
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    # tools/list
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    # tools/call
    if method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = _TOOL_DISPATCH.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool: {tool_name}"},
            }

        try:
            result = handler(**arguments)
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
                ],
            },
        }

    # ping
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def serve() -> int:
    """Run the MCP server on stdio. Blocks until stdin closes.

    Never exits non-zero on protocol errors — a misbehaving client must not
    look like a crash to the harness. Logs diagnostics to stderr so you can
    debug with:  memex mcp 2>/tmp/mcp.log
    """
    _log(f"started (pid={os.getpid()})")
    try:
        while True:
            try:
                msg = _read_message()
            except Exception as e:
                _log(f"read error: {e}")
                break

            if msg is None:
                _log("stdin closed or protocol error — exiting")
                break

            try:
                response = _handle_request(msg)
            except Exception as e:
                _log(f"handler error: {e}")
                response = {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"internal error: {e}"},
                }

            if response is not None:
                try:
                    _write_message(response)
                except Exception as e:
                    _log(f"write error: {e}")
                    break
    except KeyboardInterrupt:
        _log("interrupted")
    except BrokenPipeError:
        _log("broken pipe — client disconnected")
    except Exception as e:
        _log(f"fatal: {e}")
        return 1
    return 0


def run(args) -> int:
    """CLI entry point: `memex mcp`."""
    return serve()
