"""memex MCP server — expose the brain as tools for AI agents.

Stdlib-only JSON-RPC 2.0 over stdio (Content-Length framed). No external
dependencies — the MCP transport is simple enough to implement directly.

Tools exposed (what the agent calls mid-session):
  search   — find pages in the brain, returning structured results with paths
  remember — file one durable fact into the brain right now
  status   — peek at the brain: raw notes, wiki pages, pending, now-pages

Start:  memex mcp   (or `python -m memex.mcp_server`)
"""

from __future__ import annotations

import json
import sys
import time
from argparse import Namespace
from pathlib import Path

from . import config as config_mod
from . import ingest as ingest_mod
from . import limits as limits_mod
from . import now as now_mod
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
                       "pending synthesis, working-memory pages, and suggestions. "
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

    tiers = {}
    for p in pages:
        t = p.get("tier", "silver")
        tiers[t] = tiers.get(t, 0) + 1

    now_pages = sorted((vault / "now").glob("*.md")) if (vault / "now").is_dir() else []

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
        "tiers": tiers,
        "now_pages": [p.stem for p in now_pages],
        "suggestions": suggestions,
    }


_TOOL_DISPATCH = {
    "search": _tool_search,
    "remember": _tool_remember,
    "status": _tool_status,
}


# ── JSON-RPC over stdio ─────────────────────────────────────────────────────

def _read_message() -> dict | None:
    """Read one Content-Length-framed JSON-RPC message from stdin."""
    # Read headers until empty line
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("ascii", errors="ignore").strip()
        if not line:
            break
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    if not content_length:
        return None

    body = sys.stdin.buffer.read(content_length)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
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
    """Run the MCP server on stdio. Blocks until stdin closes."""
    try:
        while True:
            msg = _read_message()
            if msg is None:
                break
            response = _handle_request(msg)
            if response is not None:
                _write_message(response)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    return 0


def run(args) -> int:
    """CLI entry point: `memex mcp`."""
    return serve()
