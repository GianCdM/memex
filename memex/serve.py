"""memex serve — the resident brain service (claude-mem parity slice).

One long-lived process that turns the memex from "pipeline of scripts
invoked per hook" into a live, observable service:

  1. GET /          — the live viewer: an SSE stream of brain events
                      (ingest → synth apply → remember → review), zero deps
                      (a single hand-rolled HTML page, EventSource-driven).
  2. GET /events    — the raw SSE feed (JSON lines): what the viewer renders.
  3. GET /api/status — the `memex status` payload as JSON.
  4. POST /api/remember — fast-path file-one-fact: writes the raw through
                      the same ingest path as the MCP tool and KICKS a
                      reflect, so the fact compiles in seconds even when
                      no Claude session is around to run the next hook.

Serving is read-mostly: the only mutation is remember (same scrub/ledger
as every ingest) and the spawned reflect (which holds the per-vault synth
lock as always). No daemon supervisor: `memex serve` runs in the
foreground; launchd/systemd own restarts if the user wants one.
"""

from __future__ import annotations

import json
import subprocess
import time
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import canon as canon_mod
from . import changes as changes_mod
from . import config as config_mod
from . import ingest as ingest_mod
from . import mcp_server as mcp_mod
from . import proc
from . import vault as vault_mod

_VIEWER_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>memex — cérebro ao vivo</title>
<style>
 body{font:14px/1.5 -apple-system,ui-monospace,monospace;margin:0;background:#0d1117;color:#c9d1d9}
 header{padding:12px 20px;border-bottom:1px solid #21262d;display:flex;gap:16px;align-items:baseline}
 h1{font-size:15px;margin:0;color:#58a6ff} #st{font-size:12px;color:#8b949e}
 main{padding:16px 20px;display:flex;flex-direction:column;gap:6px}
 .ev{padding:8px 12px;border:1px solid #21262d;border-radius:6px;background:#161b22;
     animation:in .3s ease} @keyframes in{from{opacity:0;transform:translateY(-4px)}to{opacity:1}}
 .ev b{color:#58a6ff} .ev .t{color:#8b949e;font-size:12px;margin-right:8px}
 .create{border-left:3px solid #3fb950}.update{border-left:3px solid #d29922}
 .remember{border-left:3px solid #a371f7}.info{border-left:3px solid #58a6ff}
</style></head><body>
<header><h1>🧠 memex — cérebro ao vivo</h1><span id="st">conectando…</span></header>
<main id="feed"></main>
<script>
const feed = document.getElementById('feed'), st = document.getElementById('st');
function add(ev){
  const d = document.createElement('div');
  d.className = 'ev ' + (ev.kind || 'info');
  d.innerHTML = `<span class="t">${(ev.ts_iso||'').replace('T',' ').slice(0,19)}</span>` +
                `<b>${ev.kind||'info'}</b> ${ev.text||''}`;
  feed.prepend(d);
  while (feed.children.length > 200) feed.lastChild.remove();
}
async function boot(){
  try {
    const s = await (await fetch('/api/status')).json();
    add({kind:'info', text:`vault ${s.vault} — ${s.raw_notes} raw · ${s.wiki_pages} páginas · ` +
         `${s.pending} pendente(s) · ${s.pending_reviews} em review`});
  } catch(e) { add({kind:'info', text:'status indisponível: '+e}); }
}
const es = new EventSource('/events');
es.onopen = () => st.textContent = 'ao vivo';
es.onerror = () => { st.textContent = 'reconectando…'; };
es.onmessage = e => { try { add(JSON.parse(e.data)); } catch(_){} };
boot();
</script></body></html>"""


class _VaultFileWatcher:
    """Tail the vault's event sources by mtime — no inotify needed.

    Every poll tick compares mtimes of the event-bearing files; a change
    emits one SSE event describing WHAT changed (kind + one-line text).
    Cheap enough at 1s cadence against a handful of paths."""

    def __init__(self, vault: Path):
        self.vault = Path(vault)
        self._mtimes: dict[str, float] = {}

    def poll(self) -> list[dict]:
        events = []
        checks = {
            "index": self.vault / ".memex" / "index.json",
            "review": self.vault / ".memex" / "review" / "pending",
            "synthed": self.vault / ".memex" / "synthed.json",
            "workspace": self.vault / "workspace",
        }
        for kind, path in checks.items():
            try:
                if path.is_dir():
                    mtime = max((f.stat().st_mtime for f in path.glob("*")), default=0.0)
                elif path.exists():
                    mtime = path.stat().st_mtime
                else:
                    continue
            except OSError:
                continue
            key = str(path)
            if self._mtimes.get(key) and mtime > self._mtimes[key]:
                events.append(self._describe(kind))
            self._mtimes[key] = mtime
        # remember rows appended to changelog since the last tick → "compiled"
        try:
            mtime = (self.vault / ".memex" / "changelog.jsonl").stat().st_mtime
        except OSError:
            mtime = 0.0
        key = str(self.vault / "changelog")
        if self._mtimes.get(key) and mtime > self._mtimes[key]:
            events.append(self._latest_changelog_event())
        self._mtimes[key] = mtime
        return events

    def _describe(self, kind: str) -> dict:
        labels = {"index": "wiki index atualizado (página aplicada/mutada)",
                  "review": "fila de review mudou (novo ChangeSet pendente)",
                  "synthed": "síntese avançou (raws marcados processados)",
                  "workspace": "workspace-page atualizada"}
        return {"kind": kind, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "text": labels.get(kind, kind)}

    def _latest_changelog_event(self) -> dict:
        try:
            last = (self.vault / ".memex" / "changelog.jsonl").read_text(
                encoding="utf-8").strip().splitlines()[-1]
            rec = json.loads(last)
            from datetime import datetime
            return {"kind": rec.get("action", "update"),
                    "ts_iso": datetime.fromtimestamp(rec.get("ts", 0)).isoformat(),
                    "text": f"{rec.get('page')} ({rec.get('source')})"}
        except Exception:
            return {"kind": "update", "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "text": "changelog mudou"}


def _status_payload(vault: Path) -> dict:
    return mcp_mod._tool_status(vault=str(vault))


def make_handler(vault: Path):
    """Bind the resolved vault into the request handlers."""
    watcher = _VaultFileWatcher(vault)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request stderr noise
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                body = _VIEWER_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/status":
                self._json(_status_payload(vault))
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        for ev in watcher.poll():
                            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        time.sleep(1.0)
                except (ConnectionAbortedError, BrokenPipeError, OSError):
                    pass  # client went away — normal for SSE
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path != "/api/remember":
                return self._json({"ok": False, "error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"ok": False, "error": "invalid JSON body"}, 400)
            text = (payload.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "empty text"}, 400)
            sid = f"remember-{int(time.time())}"
            seen = ingest_mod._ledger_load(vault)
            fname = ingest_mod.ingest_session(
                vault, {"source": "remember", "id": sid,
                        "date": time.strftime("%Y-%m-%d"),
                        "cwd": payload.get("cwd") or str(vault), "text": text}, seen)
            if not fname:
                return self._json({"ok": False, "error": "nothing saved (already known)"})
            vault_mod.log_append(vault, f"remember: {text[:80]}")
            # fast-path: kick a reflect NOW so the fact compiles in seconds
            pid = proc.spawn_detached([proc.memex_exe(), "reflect", "--vault", str(vault)])
            return self._json({"ok": True, "file": f"raw/{fname}",
                               "synthesized": False, "queued": True,
                               "reflect_pid": pid})

    return Handler


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1
    host = getattr(args, "host", None) or "127.0.0.1"
    port = int(getattr(args, "port", None) or 3777)
    server = ThreadingHTTPServer((host, port), make_handler(vault))
    print(f"memex serve — brain at {vault}")
    print(f"  viewer:  http://{host}:{port}/")
    print(f"  events:  http://{host}:{port}/events   (SSE)")
    print(f"  status:  http://{host}:{port}/api/status")
    print(f"  remember: POST /api/remember {{\"text\": \"...\"}} (kicks a reflect)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        server.server_close()
    return 0
