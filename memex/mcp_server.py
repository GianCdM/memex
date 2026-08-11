"""memex MCP server — expose the brain as tools for AI agents.

Stdlib-only JSON-RPC 2.0 over newline-delimited stdio. No external
dependencies — the MCP transport is simple enough to implement directly.

MCP stdio framing is one JSON-RPC object per line. Logs always go to stderr;
stdout is reserved exclusively for protocol messages.

Tools exposed (what the agent calls mid-session):
  search          — find pages in the brain, returning structured results with paths
  remember        — file one durable fact into the brain right now
  status          — peek at the brain: raw notes, canonical wiki pages, pending, workspace-pages
  health          — report canonical wiki integrity (canonical pages, review queue, suggestions)
  audit           — scan wiki integrity and prepare reversible repairs (dry-run by default)
  review_list     — list ChangeSets in the review queue (pending by default)
  review_show     — show the full JSON of one ChangeSet
  review_approve  — approve + apply a pending ChangeSet (explicit approval)
  review_reject   — reject a pending ChangeSet with an optional reason
  review_rollback — reverse an applied ChangeSet

Start:  memex mcp   (or `python -m memex.mcp_server`)
"""

from __future__ import annotations

import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path

from . import audit as audit_mod
from . import canon as canon_mod
from . import changes as changes_mod
from . import config as config_mod
from . import ingest as ingest_mod
from . import limits as limits_mod
from . import recall as recall_mod
from . import review as review_mod
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
        "description": "Peek at the memex brain — raw notes, canonical wiki pages, pending synthesis, workspace-pages, kinds, statuses, and the review queue.",
        "inputSchema": {
            "type": "object",
            "properties": {"vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."}},
        },
    },
    {
        "name": "health",
        "description": "Report canonical wiki integrity: canonical page count, per-section breakdown, pending/stale review queue depth, invalid current identities, and suggestion counts.",
        "inputSchema": {
            "type": "object",
            "properties": {"vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."}},
        },
    },
    {
        "name": "audit",
        "description": "Scan wiki integrity and prepare reversible repairs. Defaults to DRY-RUN: writes .memex/audit/latest.{md,json} and files pending ChangeSets for lots 1 (technical identities -> reclassify review) and 2 (mechanical duplicates -> merge candidates) WITHOUT applying anything. Only pass dry_run: false to APPLY the auto-apply lots: lot 2 mechanical merges (via the reversible promoter) and lot 0 legacy artifact migration. Lot 1 is never auto-applied.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
                "dry_run": {"type": "boolean", "description": "When false, applies auto-apply lots. Default: true (never mutates the wiki).", "default": True},
                "lot": {"type": "integer", "description": "Recovery lot to run: 0 = legacy generated artifacts, 1 = technical identities, 2 = mechanical duplicates. Omit to run all three.", "enum": [0, 1, 2]},
            },
        },
    },
    {
        "name": "review_list",
        "description": "List ChangeSets in the review queue (default: pending) — id, operation, classification slug, risk, and reason.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
                "state": {"type": "string", "description": "Review state directory (pending, applied, rejected, stale, ...). Default: pending.", "default": "pending"},
            },
        },
    },
    {
        "name": "review_show",
        "description": "Show the full ChangeSet JSON for one review id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "The ChangeSet id to show."},
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
            },
            "required": ["change_id"],
        },
    },
    {
        "name": "review_approve",
        "description": "Approve and apply a pending ChangeSet (explicit approval bypasses the auto-apply gate).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "The ChangeSet id to approve."},
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
            },
            "required": ["change_id"],
        },
    },
    {
        "name": "review_reject",
        "description": "Reject a pending ChangeSet, moving it to the rejected state with an optional human reason.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "The ChangeSet id to reject."},
                "reason": {"type": "string", "description": "Optional reason for the rejection."},
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
            },
            "required": ["change_id"],
        },
    },
    {
        "name": "review_rollback",
        "description": "Roll back an applied ChangeSet, restoring the pre-apply page bytes and index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "The ChangeSet id to roll back."},
                "vault": {"type": "string", "description": "Path to the vault. Resolves automatically if omitted."},
            },
            "required": ["change_id"],
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
    # Canonical publication is no longer equivalent to processing: report the
    # ChangeSets the raw produced (applied or parked pending review) instead of
    # a `synthesized` boolean.
    changes = changes_mod.find_changesets_by_raw(vault, f"raw/{fname}")
    return {"ok": True, "file": f"raw/{fname}", "changes": changes}


def _tool_status(vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    mx = vault / ".memex"
    raw = list(canon_mod.raw_dir(vault).glob("*.md"))
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
    canonical = len(canon_mod.canonical_pages(vault, {"pages": pages}))
    pending_reviews = len(list((vault / ".memex" / "review" / "pending").glob("*.json")))
    return {"ok": True, "vault": str(vault), "raw_notes": len(raw), "synthesized": len(synthed), "pending": max(0, len(raw) - len(synthed)), "wiki_pages": canonical, "kinds": kinds, "statuses": statuses, "workspace_pages": [p.stem for p in workspace_pages], "suggestions": suggestions, "pending_reviews": pending_reviews}


def _tool_health(vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    return audit_mod.health(vault)


def _tool_audit(vault=None, dry_run=True, lot=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    # The tool defaults to dry_run=True (safe): a non-dry-run lot is only
    # applied when the caller explicitly passes dry_run: false. `quiet=True`
    # keeps the per-lot summary lines off stdout — the stdio JSON-RPC stream
    # is reserved exclusively for protocol messages (they still go to stderr).
    report = audit_mod.run_audit(vault, dry_run=bool(dry_run), lot=lot, quiet=True)
    return {"ok": True, "report": report}


def _tool_review_list(vault=None, state="pending"):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    return {"ok": True, "state": state, "changes": review_mod.list_changesets(vault, state)}


def _tool_review_show(change_id, vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    try:
        change, _ = changes_mod.load_changeset(vault, change_id)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "change": change}


def _tool_review_approve(change_id, vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    return changes_mod.apply_changeset(vault, change_id, approved=True)


def _tool_review_reject(change_id, reason=None, vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    return changes_mod.transition_changeset(vault, change_id, "rejected", reason=reason)


def _tool_review_rollback(change_id, vault=None):
    vault = _resolve_vault(vault)
    if not vault or not (vault / ".memex").exists():
        return {"ok": False, "error": "no memex vault found (run `memex init` first)"}
    return changes_mod.rollback_changeset(vault, change_id)


_TOOL_DISPATCH = {
    "search": _tool_search,
    "remember": _tool_remember,
    "status": _tool_status,
    "health": _tool_health,
    "audit": _tool_audit,
    "review_list": _tool_review_list,
    "review_show": _tool_review_show,
    "review_approve": _tool_review_approve,
    "review_reject": _tool_review_reject,
    "review_rollback": _tool_review_rollback,
}


def _log(msg: str) -> None:
    print(f"[memex mcp] {msg}", file=sys.stderr, flush=True)


def _read_message() -> dict | None:
    """Read exactly one JSON-RPC message from stdin.

    Supports either:
    - LSP-style headers with 'Content-Length: N' followed by a blank line and raw JSON of N bytes
    - Newline-delimited single-line JSON per line.
    """
    try:
        buf = sys.stdin.buffer
        # Read first non-empty line (skip leading blank lines)
        first = buf.readline()
        if not first:
            _log("stdin closed (EOF)")
            return None
        # Skip spurious blank lines
        while first in (b"\r\n", b"\n", b""):
            first = buf.readline()
            if not first:
                _log("stdin closed (EOF)")
                return None
        # If the line looks like JSON, parse it directly (newline-delimited JSON)
        s = first.decode("utf-8", errors="replace").lstrip()
        if s.startswith("{") or s.startswith("["):
            if len(first) > 10 * 1024 * 1024:
                _log("message exceeds 10MB")
                return None
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                # Try to read the rest of the line (rare) then give up
                rest = buf.readline()
                if rest:
                    combined = (first + rest).decode("utf-8", errors="replace")
                    try:
                        return json.loads(combined)
                    except Exception as e:
                        _log(f"invalid JSON line after combine: {e}")
                        return None
                _log("invalid JSON line")
                return None
        # Otherwise treat it as a header (Content-Length framing)
        # Read headers until blank line
        headers = {}
        line = s
        # first line might itself be a header
        while line.strip():
            parts = line.split(":", 1)
            if len(parts) == 2:
                headers[parts[0].strip().lower()] = parts[1].strip()
            # read next header line
            raw = buf.readline()
            if not raw:
                _log("stdin closed while reading headers")
                return None
            line = raw.decode("utf-8", errors="replace")
        # headers ended; expect Content-Length
        if "content-length" not in headers:
            # fallback: read one more line and try parse as JSON
            body_line = buf.readline()
            if not body_line:
                _log("no content after headers")
                return None
            try:
                return json.loads(body_line.decode("utf-8", errors="replace"))
            except Exception as e:
                _log(f"invalid JSON after headers fallback: {e}")
                return None
        try:
            n = int(headers["content-length"])
        except Exception:
            _log(f"invalid Content-Length: {headers.get('content-length')}")
            return None
        if n > 10 * 1024 * 1024:
            _log("message exceeds 10MB")
            return None
        body = buf.read(n)
        if not body:
            _log("stdin closed while reading body")
            return None
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except Exception as e:
            _log(f"invalid JSON body: {e}")
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
