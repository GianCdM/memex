"""memex MCP server — expose the brain as tools for AI agents.

Stdlib-only JSON-RPC 2.0 over newline-delimited stdio. No external
dependencies — the MCP transport is simple enough to implement directly.

MCP stdio framing is one JSON-RPC object per line. Logs always go to stderr;
stdout is reserved exclusively for protocol messages.

Tools exposed (what the agent calls mid-session):
  search   — find pages in the brain, returning structured results with paths
  remember — file one durable fact into the brain right now
  status   — peek at the brain: raw notes, wiki pages, pending, workspace-pages

Start:  memex mcp   (or `python -m memex.mcp_server`)
"""

from __future__ import annotations

import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path

from . import canon as canon_mod
from . import config as config_mod
from . import ingest as ingest_mod
from . import limits as limits_mod
from . import recall as recall_mod
from . import synth as synth_mod
from . import vault as vault_mod

SERVER_INFO = {"name": "memex", "version": "0.1.0"}

TOOLS = [
    {
        "name": "search",
        "description": "Search the memex brain for pages matching a query. Returns scored results with file paths — Read the path for full detail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms (keywords, not natural language)."},
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
                "limit": {"type": "integer", "description": "Maximum results (default: 5).", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "File one durable fact, decision, or preference into the memex brain. The text is synthesized into a wiki page immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "One clear, self-contained paragraph to save. Include rationale and context."},
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "status",
        "description": "Peek at the memex brain — raw notes, wiki pages, pending synthesis, workspace-pages, kinds, and statuses.",
        "inputSchema": {
            "type": "object",
            "properties": {"vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."}},
        },
    },
]


def _resolve_vault(vault_arg=None):
    try:
        return config_mod.resolve_vault(vault_arg)
    except Exception:
        return None


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
    scored = recall_mod.hybrid_rank(canon_mod.canonical_pages(vault, index), query, lim, vault, min_tokens=1, log_prefix="memex mcp")
    results = []
    for score, p in scored[:max(1, int(limit))]:
        results.append({
            "title": p.get("title", ""), "slug": p.get("slug", ""), "score": round(score, 3),
            "kind": p.get("kind", "session"), "status": p.get("status", "current"),
            "section": p.get("section", ""), "summary": " ".join((p.get("summary") or "").split())[:220],
            "tags": p.get("tags", []), "path": str(vault / "wiki" / (p.get("path") or "")),
        })
    return {"ok": True, "query": query, "total": len(scored), "results": results}


def _tool_remember(text, vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    sid = f"remember-{int(time.time())}"
    seen = ingest_mod._ledger_load(vault)
    fname = ingest_mod.ingest_session(vault, {"source": "remember", "id": sid, "date": time.strftime("%Y-%m-%d"), "cwd": str(Path.cwd()), "text": text}, seen)
    if not fname:
        return {"ok": False, "error": "nothing saved (empty or already known)"}
    vault_mod.log_append(vault, f"remember: {text[:80]}")
    synth_mod.run(Namespace(vault=str(vault), provider=None, limit=None, since=None, only=fname, model_propose=None, model_merge=None))
    try:
        synthed = json.loads((vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
    except Exception:
        synthed = {}
    return {"ok": True, "file": f"raw/{fname}", "synthesized": fname in synthed}


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
    kinds, statuses = {}, {}
    for p in pages:
        kinds[p.get("kind", "session")] = kinds.get(p.get("kind", "session"), 0) + 1
        statuses[p.get("status", "current")] = statuses.get(p.get("status", "current"), 0) + 1
    workspace_pages = sorted((vault / "workspace").glob("*.md")) if (vault / "workspace").is_dir() else []
    suggestions = 0
    sug = vault / ".memex" / "audit" / "merge-suggestions.md"
    if sug.exists():
        suggestions = sum(1 for ln in sug.read_text(encoding="utf-8").splitlines() if ln.startswith("## "))
    return {"ok": True, "vault": str(vault), "raw_notes": len(raw), "synthesized": len(synthed), "pending": max(0, len(raw) - len(synthed)), "wiki_pages": len(pages), "kinds": kinds, "statuses": statuses, "workspace_pages": [p.stem for p in workspace_pages], "suggestions": suggestions}


_TOOL_DISPATCH = {"search": _tool_search, "remember": _tool_remember, "status": _tool_status}


def _log(msg: str) -> None:
    print(f"[memex mcp] {msg}", file=sys.stderr, flush=True)


def _read_message() -> dict | None:
    """Read exactly one newline-delimited JSON-RPC message from stdin."""
    try:
        line = sys.stdin.buffer.readline()
        if not line:
            _log("stdin closed (EOF)")
            return None
        if len(line) > 10 * 1024 * 1024:
            _log("message exceeds 10MB")
            return None
        return json.loads(line)
    except json.JSONDecodeError as e:
        _log(f"invalid JSON line: {e}")
        return None
    except Exception as e:
        _log(f"read error: {type(e).__name__}: {e}")
        return None


def _write_message(data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.buffer.write(body.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _handle_request(msg: dict) -> dict | None:
    req_id = msg.get("id")
    method = msg.get("method", "")
    if req_id is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        handler = _TOOL_DISPATCH.get(params.get("name", ""))
        if not handler:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown tool: {params.get('name', '')}"}}
        try:
            result = handler(**params.get("arguments", {}))
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown method: {method}"}}


def serve() -> int:
    """Run the MCP server using newline-delimited JSON-RPC over stdio."""
    _log(f"started (pid={os.getpid()})")
    try:
        while True:
            msg = _read_message()
            if msg is None:
                break
            response = _handle_request(msg)
            if response is not None:
                _write_message(response)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except Exception as e:
        _log(f"fatal: {e}")
        return 1
    return 0


def run(args) -> int:
    return serve()
