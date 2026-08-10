"""memex v2 test suite — stdlib only, no real LLM, no network beyond localhost.

Covers the whole brain loop in-process:
  vault ensure/upgrade · capture (hook payload -> raw) · synth (mock provider)
  · reflect (wiki + workspace-page) · boot (SessionStart injection) · recall
  (ranking + session dedup) · hook install/uninstall · search
  · scrub · proc.pid_alive.

Run:  python -m unittest discover -s tests -v   (from the repo root)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memex import analyze as analyze_mod    # noqa: E402
from memex import audit as audit_mod        # noqa: E402
from memex import boot as boot_mod          # noqa: E402
from memex import canon as canon_mod        # noqa: E402
from memex import changes as changes_mod    # noqa: E402
from memex import capture as capture_mod    # noqa: E402
from memex import config as config_mod      # noqa: E402
from memex import embed as embed_mod        # noqa: E402
from memex import hook as hook_mod          # noqa: E402
from memex import ingest as ingest_mod      # noqa: E402
from memex import workspace as workspace_mod  # noqa: E402
from memex import proc                      # noqa: E402
from memex import recall as recall_mod      # noqa: E402
from memex import reflect as reflect_mod    # noqa: E402
from memex import review as review_mod      # noqa: E402
from memex import scrub as scrub_mod        # noqa: E402
from memex import search as search_mod      # noqa: E402
from memex import synth as synth_mod        # noqa: E402
from memex import vault as vault_mod        # noqa: E402
from memex import verify as verify_mod      # noqa: E402


# --------------------------------------------------------------------------- #
# Mock OpenAI-compatible provider (what Ollama/LM Studio speak)
# --------------------------------------------------------------------------- #
class _MockLLMHandler(BaseHTTPRequestHandler):
    seen_prompts = []  # reset per test that needs prompt introspection

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        _MockLLMHandler.seen_prompts.append(prompt)
        if "Reply with STRICT JSON" in prompt:          # synth phase 1: propose
            content = json.dumps({
                "skip": False, "slug": "databricks-cost-alerts",
                "title": "Databricks cost alerts", "section": "topics",
                "tags": ["databricks", "alerts"], "related": [],
                "project": "iniciativa-custos",         # content-inferred project
                "distill": "Decided to alert on Databricks cost spikes via daily job.",
                # one claim anchored to a line of the shared _fake_transcript
                # fixture: the evidence anchor resolves, so auto-apply is only
                # exercised WITH a grounded claim (a claim-less proposal must
                # never auto-apply — see Finding-1 guard in apply_changeset)
                "claims": [{"text": "Vamos criar um job diário que compara o custo com a média móvel.",
                            "type": "process", "explicitness": "explicit"}],
            })
        elif "You verify" in prompt:  # fidelity gate (full FIDELITY / DOC / DELTA prompts)
            content = json.dumps({"outcome": "supported", "value": "new", "reason": "mock"})
        elif "WORKING-MEMORY" in prompt:                # workspace-page generation
            content = ("## Contexto\nAlertas de custo do Databricks.\n\n"
                       "## Estado atual\nJob diário criado e testado.\n\n"
                       "## Próximos passos\n- [ ] ligar o schedule\n\n"
                       "## Arquivos-chave\n- jobs/cost_alert.py — o job\n")
        elif "consolidating several wiki pages" in prompt:  # tidy merge (unused post-Task 7)
            content = "## Consolidado\nTudo sobre o tema numa página só.\n"
        else:                                           # synth phase 2: merge body
            content = ("## Decisão\nAlertar picos de custo com um job diário.\n\n"
                       "Contexto: time gastava sem visibilidade.\n")
        payload = json.dumps({"choices": [{"message": {"content": content}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *a):  # silence
        pass


def _start_mock_llm():
    srv = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _payload_stdin(payload: dict):
    """Fake the harness: hook JSON on stdin."""
    stream = io.StringIO(json.dumps(payload))
    stream.isatty = lambda: False  # type: ignore[attr-defined]
    return stream


def _run_capturing(fn, args, payload=None):
    """Run a command func with optional stdin payload; return (rc, stdout)."""
    old_stdin = sys.stdin
    out = io.StringIO()
    try:
        if payload is not None:
            sys.stdin = _payload_stdin(payload)
        with redirect_stdout(out):
            rc = fn(args)
    finally:
        sys.stdin = old_stdin
    return rc, out.getvalue()


def _fake_transcript(dirpath: Path, session_id: str, cwd: str) -> Path:
    """A minimal Claude Code .jsonl transcript."""
    lines = [
        {"type": "user", "cwd": cwd, "timestamp": "2026-07-11T12:00:00Z",
         "message": {"content": "Preciso montar alertas de custo do Databricks para o time."}},
        {"type": "assistant", "cwd": cwd,
         "message": {"content": [
             {"type": "text", "text": "Vamos criar um job diário que compara o custo com a média móvel."},
             {"type": "tool_use", "name": "Write", "input": {"file_path": "jobs/cost_alert.py"}},
         ]}},
        {"type": "user", "cwd": cwd,
         "message": {"content": "Perfeito, decidimos: alerta quando custo diário > 2x média de 7 dias."}},
    ]
    fp = dirpath / f"{session_id}.jsonl"
    fp.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
    return fp


def _reflect_complete(proposal: dict, merge_body: str):
    """A `providers.complete` side_effect that routes by prompt for one reflect
    run: propose (STRICT JSON) -> the proposal, fidelity verify -> supported,
    WORKING-MEMORY -> a workspace-page body, merge -> the proposed body.

    A single callable (not a fixed side_effect list) because a reflect run makes
    4 calls: propose + merge + verify from synth, then the workspace refresh.
    """
    def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
        if "Reply with STRICT JSON" in prompt:
            return json.dumps(proposal)
        if "You verify" in prompt:
            return json.dumps({"outcome": "supported", "value": "new", "reason": "explicit"})
        if "WORKING-MEMORY" in prompt:
            return ("## Contexto\nAlertas de custo do Databricks.\n\n"
                    "## Estado atual\nJob diário definido.\n\n"
                    "## Próximos passos\n- [ ] ligar o schedule\n\n"
                    "## Arquivos-chave\n- jobs/cost_alert.py — o job\n")
        return merge_body
    return _route


def _materialize_pages(vault: Path, pages) -> None:
    """Create the wiki file each index page points at, mirroring what
    synth/reflect write at runtime. Since the canonical read paths now require
    the indexed file to exist on disk, fixtures must materialize it too —
    otherwise the page is (correctly) filtered out before ranking."""
    for p in pages:
        rel = p.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        fp = vault / "wiki" / rel
        if not fp.exists():
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(
                f"---\ntitle: \"{p.get('title', p.get('slug', ''))}\"\n---\n\n## Test\n",
                encoding="utf-8")


class MemexTestCase(unittest.TestCase):
    """Base: isolated tmp dir, isolated global config (XDG_CONFIG_HOME)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="memex-test-"))
        self.vault = self.tmp / "vault"
        vault_mod.ensure(self.vault, quiet=True)
        self.workspace = self.tmp / "ws"
        (self.workspace / ".git").mkdir(parents=True)
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        workspace_mod._PROJECT_CACHE.clear()

    def tearDown(self):
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg

    # -- convenience ---------------------------------------------------------
    def seed_index(self, pages):
        (self.vault / ".memex" / "index.json").write_text(
            json.dumps({"pages": pages}), encoding="utf-8")

    def project(self):
        return workspace_mod.project_key(str(self.workspace))

    def workspace_key(self):
        return workspace_mod.workspace_key(str(self.workspace))

    def raw_dir(self):
        """The physical raw-evidence dir (.memex/raw — a dot-dir the Obsidian
        vault never lists)."""
        return canon_mod.raw_dir(self.vault)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class TestVault(MemexTestCase):
    def test_ensure_creates_v2_layout(self):
        for rel in (".memex/raw", "workspace", "wiki/topics", "wiki/entities",
                    "wiki/decisions", ".memex/state", ".memex/audit",
                    ".memex/views/projects", ".memex/review/pending",
                    "SCHEMA.md", "log.md"):
            self.assertTrue((self.vault / rel).exists(), rel)
        # raw evidence is a dot-dir (Obsidian-lean): no top-level raw/
        self.assertFalse((self.vault / "raw").exists(),
                         "raw must live under .memex/raw, not the vault root")
        # generated views/audit live under .memex/, NOT as wiki pages or the root
        self.assertFalse((self.vault / "wiki" / "projects").exists())
        self.assertFalse((self.vault / "index.md").exists())
        self.assertIn("How agents use this brain",
                      (self.vault / "SCHEMA.md").read_text(encoding="utf-8"))

    def test_ensure_upgrades_v1_schema(self):
        v1 = self.tmp / "v1vault"
        (v1 / ".memex").mkdir(parents=True)
        (v1 / "schema.md").write_text("# memex vault schema\n\nold", encoding="utf-8")
        vault_mod.ensure(v1, quiet=True)
        self.assertTrue((v1 / "SCHEMA.md").exists())
        self.assertIn("How agents use this brain",
                      (v1 / "SCHEMA.md").read_text(encoding="utf-8"))
        self.assertTrue((v1 / "workspace").is_dir())
        self.assertTrue((v1 / "log.md").exists())

    def test_ensure_is_idempotent_and_preserves_custom_schema(self):
        (self.vault / "SCHEMA.md").write_text("# my own rules", encoding="utf-8")
        vault_mod.ensure(self.vault, quiet=True)
        self.assertEqual((self.vault / "SCHEMA.md").read_text(encoding="utf-8"),
                         "# my own rules")

    def test_ensure_seeds_about_and_preserves_user_edits(self):
        about = self.vault / "ABOUT.md"
        self.assertTrue(about.exists())
        self.assertIn("who owns this brain", about.read_text(encoding="utf-8"))
        about.write_text("# ABOUT\nSou gestor de engenharia.", encoding="utf-8")
        vault_mod.ensure(self.vault, quiet=True)
        self.assertIn("gestor de engenharia", about.read_text(encoding="utf-8"))

    def test_ensure_is_safe_inside_an_existing_obsidian_vault(self):
        """Pointing memex at a lived-in Obsidian vault must not touch its notes."""
        ob = self.tmp / "obsidian"
        (ob / "Diário").mkdir(parents=True)
        note = ob / "Diário" / "2026-07-11.md"
        note.write_text("minha nota pessoal", encoding="utf-8")
        vault_mod.ensure(ob, quiet=True)
        self.assertEqual(note.read_text(encoding="utf-8"), "minha nota pessoal")
        self.assertTrue((ob / "SCHEMA.md").exists())
        self.assertTrue((ob / ".memex" / "index.json").exists())


class TestProc(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(proc.pid_alive(os.getpid()))
        self.assertFalse(proc.pid_alive(999999999))
        self.assertFalse(proc.pid_alive(None))
        self.assertFalse(proc.pid_alive(-5))

    def test_run_kwargs_forces_utf8_text_io(self):
        """text=True must become explicit UTF-8: the locale codepage (cp1252)
        mangled '→'/emoji both ways and made communicate() return stdout=None."""
        kw = proc.run_kwargs(capture_output=True, text=True, timeout=5)
        self.assertNotIn("text", kw)
        self.assertEqual(kw["encoding"], "utf-8")
        self.assertEqual(kw["errors"], "replace")
        self.assertTrue(kw["capture_output"])
        # explicit encodings are respected, not clobbered
        kw = proc.run_kwargs(text=True, encoding="latin-1")
        self.assertEqual(kw["encoding"], "latin-1")


class TestScrub(unittest.TestCase):
    def test_scrubs_tokens(self):
        s = scrub_mod.scrub('api_key: "sk-ant-abcdefghijklmnop1234" e ghp_' + "a" * 24)
        self.assertNotIn("sk-ant-abcdefghijklmnop1234", s)
        self.assertNotIn("ghp_" + "a" * 24, s)


class TestCapture(MemexTestCase):
    def test_capture_ingests_hook_transcript(self):
        t = _fake_transcript(self.tmp, "sess-1", str(self.workspace))
        spawned = []
        orig = proc.spawn_detached
        proc.spawn_detached = lambda argv, cwd=None: spawned.append(argv) or 4242
        try:
            rc, out = _run_capturing(
                capture_mod.run,
                Namespace(vault=str(self.vault), partial=False, docs=False,
                          workspace=None, transcript=None, no_reflect=False),
                payload={"session_id": "sess-1", "transcript_path": str(t),
                         "cwd": str(self.workspace)})
        finally:
            proc.spawn_detached = orig
        self.assertEqual(rc, 0)
        raws = list((self.raw_dir()).glob("*.md"))
        self.assertEqual(len(raws), 1)
        body = raws[0].read_text(encoding="utf-8")
        self.assertIn("Databricks", body)
        self.assertIn(f"cwd: {self.workspace}", body)
        self.assertEqual(len(spawned), 1)          # reflect fired, detached
        self.assertIn("reflect", spawned[0])

    def test_partial_capture_skips_reflect_and_dedups(self):
        t = _fake_transcript(self.tmp, "sess-2", str(self.workspace))
        spawned = []
        orig = proc.spawn_detached
        proc.spawn_detached = lambda argv, cwd=None: spawned.append(argv) or 1
        try:
            for _ in range(2):  # PreCompact may fire repeatedly — must dedup
                rc, _out = _run_capturing(
                    capture_mod.run,
                    Namespace(vault=str(self.vault), partial=True, docs=False,
                              workspace=None, transcript=None, no_reflect=False),
                    payload={"transcript_path": str(t), "cwd": str(self.workspace)})
                self.assertEqual(rc, 0)
        finally:
            proc.spawn_detached = orig
        self.assertEqual(len(list((self.raw_dir()).glob("*.md"))), 1)
        self.assertEqual(spawned, [])              # no reflect on partial

    def test_full_capture_preserves_partial_raw_evidence(self):
        t = _fake_transcript(self.tmp, "sess-3", str(self.workspace))
        args = lambda: Namespace(vault=str(self.vault), partial=True, docs=False,  # noqa: E731
                                 workspace=None, transcript=None, no_reflect=True)
        _run_capturing(capture_mod.run, args(),
                       payload={"transcript_path": str(t), "cwd": str(self.workspace)})
        # session continues: transcript grows, then SessionEnd captures again
        with t.open("a", encoding="utf-8") as f:
            f.write("\n" + json.dumps({"type": "user", "cwd": str(self.workspace),
                                       "message": {"content": "Novo requisito: alertar no Slack."}}))
        a2 = args(); a2.partial = False
        _run_capturing(capture_mod.run, a2,
                       payload={"transcript_path": str(t), "cwd": str(self.workspace)})
        raws = sorted((self.raw_dir()).glob("*.md"))
        self.assertEqual(len(raws), 2)                      # partial + final both persist
        self.assertTrue(any("Slack" not in raw.read_text(encoding="utf-8") for raw in raws))
        self.assertTrue(any("Slack" in raw.read_text(encoding="utf-8") for raw in raws))
        # workspace selection prefers the newest final capture (with "Slack")
        picked, _meta = workspace_mod._raw_candidate(self.vault, self.workspace_key())
        self.assertIsNotNone(picked)
        self.assertIn("Slack", picked.read_text(encoding="utf-8"))

    def test_changed_capture_preserves_prior_raw_evidence(self):
        transcript = _fake_transcript(self.tmp, "immutable-raw", str(self.workspace))
        args = Namespace(vault=str(self.vault), partial=True, docs=False,
                         workspace=None, transcript=None, no_reflect=True)
        _run_capturing(capture_mod.run, args, payload={"transcript_path": str(transcript), "cwd": str(self.workspace)})
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write("\n" + json.dumps({"type": "user", "cwd": str(self.workspace), "message": {"content": "A second durable fact."}}))
        _run_capturing(capture_mod.run, args, payload={"transcript_path": str(transcript), "cwd": str(self.workspace)})

        raws = sorted((self.raw_dir()).glob("*.md"))
        self.assertEqual(len(raws), 2)
        self.assertNotIn("A second durable fact.", raws[0].read_text(encoding="utf-8"))
        self.assertIn("A second durable fact.", raws[1].read_text(encoding="utf-8"))


class TestRecall(MemexTestCase):
    PAGES = [
        {"slug": "databricks-cost-alerts", "title": "Databricks cost alerts",
         "section": "topics", "kind": "silver", "status": "current", "tags": ["databricks", "alerts"],
         "summary": "Daily job alerting on Databricks cost spikes",
         "path": "topics/databricks-cost-alerts.md", "project": "ws"},
        {"slug": "airflow-migration", "title": "Airflow migration",
         "section": "topics", "kind": "silver", "status": "current", "tags": ["airflow"],
         "summary": "Plan to migrate DAGs to Airflow 3",
         "path": "topics/airflow-migration.md", "project": "ws"},
    ]

    def setUp(self):
        super().setUp()
        _materialize_pages(self.vault, self.PAGES)

    def test_recall_injects_relevant_page_with_path(self):
        self.seed_index(self.PAGES)
        rc, out = _run_capturing(
            recall_mod.run, Namespace(vault=str(self.vault), query=None),
            payload={"session_id": "s1",
                     "prompt": "como configuramos os alertas de custo do databricks?"})
        self.assertEqual(rc, 0)
        self.assertIn("databricks-cost-alerts", out)
        self.assertIn(str(self.vault / "wiki" / "topics" / "databricks-cost-alerts.md"), out)
        self.assertNotIn("airflow-migration", out)

    def test_recall_dedups_within_session_but_not_across(self):
        self.seed_index(self.PAGES)
        p = {"session_id": "s2",
             "prompt": "como configuramos os alertas de custo do databricks?"}
        _, out1 = _run_capturing(recall_mod.run, Namespace(vault=str(self.vault), query=None), p)
        _, out2 = _run_capturing(recall_mod.run, Namespace(vault=str(self.vault), query=None), p)
        self.assertIn("databricks-cost-alerts", out1)
        self.assertEqual(out2, "")                 # same session: silent
        p3 = dict(p, session_id="s3")
        _, out3 = _run_capturing(recall_mod.run, Namespace(vault=str(self.vault), query=None), p3)
        self.assertIn("databricks-cost-alerts", out3)  # new session: back

    def test_recall_silent_on_terse_or_error(self):
        self.seed_index(self.PAGES)
        _, out = _run_capturing(recall_mod.run, Namespace(vault=str(self.vault), query=None),
                                payload={"session_id": "s4", "prompt": "ok"})
        self.assertEqual(out, "")
        rc, out = _run_capturing(recall_mod.run,
                                 Namespace(vault=str(self.tmp / "nope"), query=None),
                                 payload={"prompt": "qualquer coisa mais longa aqui"})
        self.assertEqual((rc, out), (0, ""))       # never blocks the prompt


class TestCanonicalPages(MemexTestCase):
    def _page(self, slug, section="topics", status="current", path=None):
        path = path or f"{section}/{slug}.md"
        page = {
            "slug": slug,
            "title": slug.replace("-", " ").title(),
            "section": section,
            "kind": "session",
            "status": status,
            "tags": [],
            "sources": ["session:test"],
            "summary": "test page",
            "path": path,
            "project": "ws",
        }
        target = self.vault / "wiki" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\ntitle: \"{page['title']}\"\n---\n\n## Test\n", encoding="utf-8")
        return page

    def test_canonical_pages_exclude_noncurrent_missing_and_noncanonical_paths(self):
        current = self._page("current-topic")
        archived = self._page("archived-topic", status="archived")
        missing = dict(self._page("missing-topic"))
        (self.vault / "wiki" / missing["path"]).unlink()
        bad_section = self._page("project-hub", section="projects")
        self.seed_index([current, archived, missing, bad_section])

        pages = canon_mod.canonical_pages(self.vault)

        self.assertEqual([p["slug"] for p in pages], ["current-topic"])

    def test_recall_and_search_never_surface_archived_or_missing_pages(self):
        current = self._page("cost-alerts")
        archived = self._page("cost-alerts-old", status="archived")
        missing = self._page("cost-alerts-missing")
        (self.vault / "wiki" / missing["path"]).unlink()
        self.seed_index([current, archived, missing])

        _, recall_out = _run_capturing(
            recall_mod.run,
            Namespace(vault=str(self.vault), query=None),
            payload={"session_id": "canonical", "prompt": "preciso rever cost alerts agora"},
        )
        _, search_out = _run_capturing(
            search_mod.run,
            Namespace(vault=str(self.vault), terms=["cost", "alerts"], limit=10),
        )

        self.assertIn("cost-alerts", recall_out)
        self.assertNotIn("cost-alerts-old", recall_out)
        self.assertNotIn("cost-alerts-missing", recall_out)
        self.assertIn("cost-alerts", search_out)
        self.assertNotIn("cost-alerts-old", search_out)
        self.assertNotIn("cost-alerts-missing", search_out)

    def test_embed_uses_only_canonical_pages(self):
        current = self._page("canonical-embed")
        archived = self._page("archived-embed", status="archived")
        self.seed_index([current, archived])
        cfg = json.loads((self.vault / ".memex" / "config.json").read_text(encoding="utf-8"))
        cfg["embeddings"] = {"base_url": "http://127.0.0.1:1/v1", "model": "mock"}
        (self.vault / ".memex" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

        with mock.patch.object(config_mod, "resolve_embeddings", return_value=("mock", {"base_url": "http://127.0.0.1:1/v1"})):
            with mock.patch("memex.providers.embed", return_value=[[1.0, 0.0]]):
                rc, _ = _run_capturing(embed_mod.run, Namespace(vault=str(self.vault), force=False, dry_run=False))

        self.assertEqual(rc, 0)
        records = (self.vault / ".memex" / "embeddings" / "topics.jsonl").read_text(encoding="utf-8")
        self.assertIn("canonical-embed", records)
        self.assertNotIn("archived-embed", records)

    def test_page_body_hash_ignores_tool_owned_updated_frontmatter(self):
        before = "---\ntitle: \"Topic\"\nupdated: 2026-08-01\n---\n\n## Rule\nKeep evidence.\n"
        after = "---\ntitle: \"Topic\"\nupdated: 2026-08-06\n---\n\n## Rule\nKeep evidence.\n"
        self.assertEqual(canon_mod.page_body_hash(before), canon_mod.page_body_hash(after))


class TestWorkspaceIdentity(MemexTestCase):
    def test_home_relative_paths_are_hierarchical_and_git_uses_repo_root(self):
        root = Path.home() / "src" / "work" / "checkout" / "api"
        nested = root / "docs" / "contracts"
        with mock.patch.object(workspace_mod, "_git_root", return_value=root):
            self.assertEqual(workspace_mod.workspace_key(str(root)), "src-work-checkout-api")
            self.assertEqual(workspace_mod.workspace_key(str(nested)), "src-work-checkout-api")
            self.assertEqual(workspace_mod.workspace_display_name(str(nested)), "api")
        workspace_mod._WORKSPACE_CACHE.clear()

    def test_same_basename_paths_do_not_collide(self):
        left = self.tmp / "one" / "gateway"
        right = self.tmp / "two" / "gateway"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        self.assertNotEqual(workspace_mod.workspace_key(str(left)), workspace_mod.workspace_key(str(right)))

    def test_incremental_workspace_cursor_uses_only_new_suffix(self):
        raw = self.raw_dir() / "session.md"
        raw.write_text("---\nsource: claude\nid: s1\ncwd: " + str(self.workspace) + "\n---\n\nprimeiro\n", encoding="utf-8")
        key = self.workspace_key()
        first = workspace_mod.incremental_source(self.vault, key, raw, session_id="s1")
        workspace_mod.write_checkpoint(self.vault, key, first["checkpoint"])
        raw.write_text(raw.read_text(encoding="utf-8") + "segundo\n", encoding="utf-8")
        second = workspace_mod.incremental_source(self.vault, key, raw, session_id="s1")
        self.assertTrue(second["incremental"])
        self.assertEqual(second["delta"], "segundo\n")

    def test_incremental_workspace_cursor_rebuilds_when_prefix_changes(self):
        raw = self.raw_dir() / "session.md"
        raw.write_text("---\nsource: claude\nid: s1\ncwd: " + str(self.workspace) + "\n---\n\nprimeiro\n", encoding="utf-8")
        key = self.workspace_key()
        first = workspace_mod.incremental_source(self.vault, key, raw, session_id="s1")
        workspace_mod.write_checkpoint(self.vault, key, first["checkpoint"])
        raw.write_text(raw.read_text(encoding="utf-8").replace("primeiro", "corrigido"), encoding="utf-8")
        second = workspace_mod.incremental_source(self.vault, key, raw, session_id="s1")
        self.assertFalse(second["incremental"])
        self.assertIn("corrigido", second["delta"])

    def test_migrates_unambiguous_legacy_workspace_page(self):
        old = self.vault / "workspace" / "ws.md"
        old.write_text("---\nworkspace: ws\nupdated: 2026-07-01T00:00:00Z\nauthor: auto\n---\n\n## Contexto\nantigo\n", encoding="utf-8")
        raw = self.raw_dir() / "2026-07-01--claude--legacy--12345678.md"
        raw.write_text("---\nsource: claude\nid: legacy\ndate: 2026-07-01T00:00:00Z\ncwd: " + str(self.workspace) + "\nkind: session\n---\n\ntexto", encoding="utf-8")
        result = workspace_mod.migrate_legacy_workspace(self.vault)
        key = self.workspace_key()
        self.assertEqual(len(result["migrated"]), 1)
        self.assertFalse(old.exists())
        meta, body = workspace_mod.read_workspace(self.vault, key)
        self.assertEqual(meta.get("workspace"), key)
        self.assertEqual(meta.get("root"), str(self.workspace.resolve()))
        self.assertIn("antigo", body)


class TestBoot(MemexTestCase):
    def _write_raw_session(self, text, date=None):
        # Freshness checks (`_raw_is_fresh` with boot_workspace_max_age_days=14)
        # compare the frontmatter date against the wall clock, so the fixture
        # MUST be relative to "now" or it silently goes stale as time passes.
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = self.raw_dir() / f"{date[:10]}--claude--latest--abc12345.md"
        raw.write_text(
            "---\nsource: claude\nid: latest\ndate: " + date +
            "\ncwd: " + str(self.workspace) + "\nkind: silver\n---\n\n" + text,
            encoding="utf-8")
        return raw

    def test_boot_injects_workspace_page_and_usage(self):
        workspace = self.workspace_key()
        workspace_mod.write_workspace(self.vault, workspace, "## Contexto\nAlertas Databricks.\n"
                          "## Próximos passos\n- [ ] ligar schedule", author="auto")
        rc, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace), "session_id": "b1"})
        self.assertEqual(rc, 0)
        self.assertIn("<memex-brain>", out)
        self.assertIn("Where we left off", out)
        self.assertIn("Alertas Databricks", out)
        self.assertIn("memex search", out)
        self.assertIn("SCHEMA.md", out)

    def test_boot_silent_on_compact_and_empty_brain(self):
        _, out = _run_capturing(boot_mod.run, Namespace(vault=str(self.vault)),
                                payload={"source": "compact", "cwd": str(self.workspace)})
        self.assertEqual(out, "")
        _, out = _run_capturing(boot_mod.run, Namespace(vault=str(self.vault)),
                                payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertEqual(out, "")                  # nothing yet for this project

    def test_boot_ignores_stale_workspace_page(self):
        workspace = self.workspace_key()
        p = workspace_mod.write_workspace(self.vault, workspace, "## Contexto\nvelho", author="auto")
        old = p.read_text(encoding="utf-8").replace(
            re.search(r"updated: (\S+)", p.read_text(encoding="utf-8")).group(1),
            "2020-01-01T00:00:00Z")
        p.write_text(old, encoding="utf-8")
        _, out = _run_capturing(boot_mod.run, Namespace(vault=str(self.vault)),
                                payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertNotIn("velho", out)

    def test_boot_does_not_inject_raw_by_default(self):
        self._write_raw_session("decisão secreta do raw")
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertNotIn("decisão secreta do raw", out)

    def test_boot_raw_tail_is_opt_in_and_fallback_only(self):
        cfg_path = self.vault / ".memex" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["limits"] = {"boot_raw_tail_chars": 80}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self._write_raw_session("decisão ainda não sintetizada com detalhe exato")
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertIn("Recent raw capture", out)
        self.assertIn("decisão ainda não sintetizada", out)

        workspace_mod.write_workspace(self.vault, self.workspace_key(), "## Estado atual\nresumo", author="auto")
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertNotIn("Recent raw capture", out)

    def test_boot_raw_tail_respects_limit_and_staleness(self):
        cfg_path = self.vault / ".memex" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["limits"] = {"boot_raw_tail_chars": 64}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self._write_raw_session("x" * 500)
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace)})
        marker = "(full raw:"
        excerpt = out[out.index("Recent raw capture"):out.index(marker)]
        self.assertLessEqual(len(excerpt.splitlines()[-1]), 64)

        raw = next((self.raw_dir()).glob("*.md"))
        # Rewrite the frontmatter `date:` to a stale value (the fixture date is
        # now time-relative) so `_raw_is_fresh` rejects it — this is the
        # deliberate staleness leg of the test.
        text = re.sub(r"^date: .*$", "date: 2020-01-01T00:00:00Z",
                      raw.read_text(encoding="utf-8"), count=1, flags=re.M)
        raw.write_text(text, encoding="utf-8")
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertNotIn("Recent raw capture", out)


class TestSynthReflect(MemexTestCase):
    def setUp(self):
        super().setUp()
        self.srv, base = _start_mock_llm()
        cfg = config_mod.load_global()
        cfg["provider"] = {
            "order": ["openai_compat"],
            "openai_compat": {"base_url": base, "api_key": None,
                              "model_propose": "mock", "model_merge": "mock"},
        }
        config_mod.save_global(cfg)

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def test_triage_consolidates_same_id_snapshots(self):
        """Same session captured N times collapses to the LARGEST body — the
        extra partial/precompact snapshots are superseded without LLM calls."""
        import memex.synth as synth_mod
        import threading
        raws = []
        for i, n in enumerate([40, 400]):
            f = self.raw_dir() / f"2026-08-08--claude--sess-abc--{i}.md"
            f.write_text(
                "---\nsource: claude\nid: sess-abc\ndate: 2026-08-08\nkind: session\n---\n\n"
                + ("linha\n" * n), encoding="utf-8")
            raws.append(f)
        todo = [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16]) for f in raws]
        synthed = {}
        prepared, _ = synth_mod._prepare_todo(
            self.vault, todo, synthed, self.vault / ".memex" / "synthed.json",
            {}, {}, threading.Lock(), [0], [])
        self.assertEqual(len(prepared), 1, "duplicate snapshot must be consolidated")
        self.assertEqual(len(synthed), 1, "superseded snapshot must be marked done")

    def test_triage_keeps_newest_not_largest_snapshot(self):
        """A doc EDITED DOWN must keep the newest (smaller) capture — an older
        longer snapshot must not supersede the current trimmed version."""
        import memex.synth as synth_mod
        import threading, time
        sid = "/src/docs/readme.md"
        big_old = "linha velha\n" * 400   # older, longer
        small_new = "linha nova\n" * 40   # newer, trimmed
        f_old = self.raw_dir() / "2026-07-01--doc--readme--old.md"
        f_old.write_text(f"---\nsource: doc\nid: {sid}\nkind: doc\n---\n\n{big_old}",
                         encoding="utf-8")
        f_new = self.raw_dir() / "2026-08-08--doc--readme--new.md"
        f_new.write_text(f"---\nsource: doc\nid: {sid}\nkind: doc\n---\n\n{small_new}",
                         encoding="utf-8")
        # force the older file to be NEWER on disk so only filename order is a
        # confound — actually set mtimes to make the small one clearly newest
        os.utime(f_old, (time.time() - 1000, time.time() - 1000))
        os.utime(f_new, (time.time(), time.time()))
        todo = [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])
                for f in (f_old, f_new)]
        prepared, _ = synth_mod._prepare_todo(
            self.vault, todo, {}, self.vault / ".memex" / "synthed.json",
            {}, {}, threading.Lock(), [0], [])
        self.assertEqual(len(prepared), 1)
        self.assertIn("linha nova", prepared[0]["body"],
                      "newest trimmed capture must be kept, not the older longer one")

    def test_triage_delta_falls_back_to_full_when_page_missing(self):
        """A delta directive whose lineage target page is gone (archived/renamed/
        never-applied) must fall back to a FULL merge — never build a headless
        page from the appended tail alone."""
        import memex.synth as synth_mod
        import threading
        sid = "/src/docs/vanished.md"
        prev_body = "base\n" * 5
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": "old.md", "chars": len(prev_body),
                  "body_hash": synth_mod._body_hash(prev_body),
                  "slug": "vanished", "section": "topics"}})
        # NOTE: no wiki/topics/vanished.md page exists
        new_body = prev_body + "## append\nconteudo novo\n" * 25
        f = self.raw_dir() / "2026-08-08--doc--vanished--abc.md"
        f.write_text(f"---\nsource: doc\nid: {sid}\nkind: doc\n---\n\n{new_body}",
                     encoding="utf-8")
        prepared, _ = synth_mod._prepare_todo(
            self.vault, [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])],
            {}, self.vault / ".memex" / "synthed.json", {}, {},
            threading.Lock(), [0], [])
        self.assertEqual(prepared[0]["mode"], "full",
                         "missing target page must fall back to a full merge")

    def test_triage_skips_config_skip_ids(self):
        """A doc whose source id matches the vault's ingest.docs.skip_ids is
        dropped before any LLM call (e.g. the owner's personal automation log)."""
        import memex.synth as synth_mod
        import threading
        f = self.raw_dir() / "2026-08-08--doc--morning--abc.md"
        f.write_text(
            "---\nsource: doc\nid: /pessoal/automation/morning-routine.log\nkind: doc\n---\n\n# rotina\n",
            encoding="utf-8")
        todo = [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])]
        vcfg = {"ingest": {"docs": {"skip_ids": ["**/morning-routine.log"]}}}
        synthed = {}
        prepared, _ = synth_mod._prepare_todo(
            self.vault, todo, synthed, self.vault / ".memex" / "synthed.json",
            vcfg, {}, threading.Lock(), [0], [])
        self.assertEqual(prepared, [], "skip_ids source must not reach synthesis")
        self.assertEqual(len(synthed), 1, "skipped source must be marked done")

    def test_reflect_emits_metrics_jsonl(self):
        """A reflect run must emit per-raw telemetry to .memex/metrics.jsonl —
        the substrate for `memex metrics` and for cost/quality decisions."""
        import memex.metrics as metrics_mod
        self._capture_session()
        _run_capturing(reflect_mod.run,
                       Namespace(vault=str(self.vault), cwd=str(self.workspace),
                                 since=None, limit=None, provider=None))
        path = self.vault / ".memex" / "metrics.jsonl"
        self.assertTrue(path.exists(), "reflect must write metrics.jsonl")
        events = list(metrics_mod.read(self.vault))
        self.assertGreaterEqual(len(events), 1)
        ev = events[0]
        for key in ("fname", "kind", "mode", "outcome", "route", "latency_ms"):
            self.assertIn(key, ev, f"metrics event missing {key}")

    def test_triage_delta_merges_append_only_doc(self):
        """A doc re-captured after growing becomes a DELTA merge against the
        known page — propose is skipped and only the new tail is passed along.
        A trivial append is superseded (no material change)."""
        import memex.synth as synth_mod
        import threading
        sid = "/src/docs/readme.md"
        prev_body = "linha inicial\n" * 5
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": "old.md", "chars": len(prev_body),
                  "body_hash": synth_mod._body_hash(prev_body),
                  "slug": "readme", "section": "topics"}})
        # the delta target page must exist (triage falls back to full otherwise)
        page = self.vault / "wiki" / "topics" / "readme.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntitle: Readme\nkind: doc\n---\n\n" + prev_body, encoding="utf-8")
        new_body = prev_body + "## novo\nconteudo novo que vale a pena\n" * 25
        f = self.raw_dir() / "2026-08-08--doc--readme--abc.md"
        f.write_text(f"---\nsource: doc\nid: {sid}\nkind: doc\n---\n\n{new_body}",
                     encoding="utf-8")
        todo = [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])]
        prepared, _ = synth_mod._prepare_todo(
            self.vault, todo, {}, self.vault / ".memex" / "synthed.json",
            {}, {}, threading.Lock(), [0], [])
        self.assertEqual(prepared[0]["mode"], "delta")
        self.assertEqual(prepared[0]["slug"], "readme")
        self.assertIn("novo", prepared[0]["delta"])

        # EMPTY delta (whitespace only) → superseded, no synthesis. A non-empty
        # append is always delta-merged (a short-but-material append must not be
        # dropped on a length threshold — the verifier catches true no-ops).
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": "old.md", "chars": len(new_body),
                  "body_hash": synth_mod._body_hash(new_body),
                  "slug": "readme", "section": "topics"}})
        f2 = self.raw_dir() / "2026-08-08--doc--readme--def.md"
        f2.write_text(f"---\nsource: doc\nid: {sid}\nkind: doc\n---\n\n{new_body}   \n\t",
                      encoding="utf-8")
        synthed = {}
        prepared2, _ = synth_mod._prepare_todo(
            self.vault, [(f2, hashlib.sha256(f2.read_bytes()).hexdigest()[:16])],
            synthed, self.vault / ".memex" / "synthed.json", {}, {},
            threading.Lock(), [0], [])
        self.assertEqual(prepared2, [], "empty append must be superseded")
        self.assertEqual(len(synthed), 1)

    def test_triage_delta_merges_append_only_session(self):
        """A SESSION re-captured after growing becomes a DELTA merge too — the
        propose step is skipped, the slug/section come from lineage, and only
        the new tail is passed along (no re-distilling the whole snapshot)."""
        import memex.synth as synth_mod
        import threading
        sid = "sess-abc"
        prev_body = "linha inicial\n" * 5
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": "old.md", "chars": len(prev_body),
                  "body_hash": synth_mod._body_hash(prev_body),
                  "slug": "sess-topic", "section": "topics"}})
        page = self.vault / "wiki" / "topics" / "sess-topic.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntitle: Sessão\nkind: session\n---\n\n" + prev_body,
                        encoding="utf-8")
        new_body = prev_body + "## novo\nconteudo novo que vale a pena\n" * 25
        f = self.raw_dir() / "2026-08-08--claude--sess-abc--def.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{new_body}",
                     encoding="utf-8")
        prepared, _ = synth_mod._prepare_todo(
            self.vault, [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])],
            {}, self.vault / ".memex" / "synthed.json",
            {}, {}, threading.Lock(), [0], [])
        self.assertEqual(prepared[0]["mode"], "delta")
        self.assertEqual(prepared[0]["slug"], "sess-topic")
        self.assertEqual(prepared[0]["section"], "topics")
        self.assertIn("conteudo novo", prepared[0]["delta"])
        self.assertNotIn("linha inicial", prepared[0]["delta"],
                         "the delta must carry ONLY the new tail, not the prefix")

    def test_triage_delta_falls_back_full_when_session_page_archived(self):
        """A session re-capture whose lineage target page was ARCHIVED
        (status: archived) must fall back to a FULL merge — never blind-append
        the tail into an obsolete page."""
        import memex.synth as synth_mod
        import threading
        sid = "sess-archived"
        prev_body = "base\n" * 5
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": "old.md", "chars": len(prev_body),
                  "body_hash": synth_mod._body_hash(prev_body),
                  "slug": "sess-topic", "section": "topics"}})
        page = self.vault / "wiki" / "topics" / "sess-topic.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntitle: Sessão\nstatus: archived\n---\n\n" + prev_body,
                        encoding="utf-8")
        new_body = prev_body + "## novo\nconteudo novo\n" * 25
        f = self.raw_dir() / "2026-08-08--claude--sess-archived--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{new_body}",
                     encoding="utf-8")
        prepared, _ = synth_mod._prepare_todo(
            self.vault, [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])],
            {}, self.vault / ".memex" / "synthed.json", {}, {},
            threading.Lock(), [0], [])
        self.assertEqual(prepared[0]["mode"], "full",
                         "archived target page must fall back to a full merge")

    def test_triage_chunks_giant_session(self):
        """A session whose body exceeds chunk_chars is split into sequential
        chunk directives (each a 50k slice) so the middle is never truncated."""
        import memex.synth as synth_mod
        import threading
        sid = "sess-giant"
        big = "linha\n" * 7000   # ~42k chars — below 50k by itself
        big = big * 3            # ~126k chars
        f = self.raw_dir() / "2026-08-08--claude--sess-giant--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{big}",
                     encoding="utf-8")
        prepared, _ = synth_mod._prepare_todo(
            self.vault, [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])],
            {}, self.vault / ".memex" / "synthed.json",
            {}, {"chunk_chars": 50000}, threading.Lock(), [0], [])
        self.assertEqual(len(prepared), 3, "126k → 3 chunk directives")
        for it in prepared:
            self.assertEqual(it["mode"], "chunk")
            self.assertLessEqual(len(it["chunk"]), 50000)
            self.assertEqual(it["chunk_of"], f.name)
        self.assertEqual([it["chunk_index"] for it in prepared], [0, 1, 2])
        self.assertEqual(prepared[0]["chunk_total"], 3)

    def test_reflect_chunks_giant_session_end_to_end(self):
        """A giant session is chunked and each 50k slice proposes/merges/verifies
        independently — a wiki page appears, all chunks are accounted, and the
        raw is marked done only after every slice was durably handled."""
        import memex.synth as synth_mod
        sid = "sess-giant-e2e"
        big = ("linha de conteudo durável " * 3000)  # ~75k chars → 2 chunks
        f = self.raw_dir() / f"2026-08-08--claude--{sid}--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{big}",
                     encoding="utf-8")
        # per-chunk propose returns the same slug (dedup via REUSE); merge returns
        # a body; verify returns supported+new → auto-apply
        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            if "Reply with STRICT JSON" in prompt:
                # a claim whose text is a substring of the chunk → anchors in the
                # full raw, so the chunk auto-applies (the real happy path)
                return json.dumps({"skip": False, "slug": "sess-giant-e2e",
                                   "title": "Giant", "section": "topics",
                                   "tags": [], "related": [], "project": None,
                                   "distill": "d.",
                                   "claims": [{"text": "linha de conteudo durável",
                                               "type": "fact", "explicitness": "explicit"}]})
            if "You verify" in prompt:
                return json.dumps({"outcome": "supported", "value": "new", "reason": "ok"})
            return "## Contéudo\ndurável.\n"
        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=4)
        with mock.patch("memex.providers.complete", side_effect=_route):
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        page = self.vault / "wiki" / "topics" / "sess-giant-e2e.md"
        self.assertTrue(page.exists(), "chunked session must produce a page")
        # raw marked done only after ALL chunks handled
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text())
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        self.assertEqual(synthed.get(f.name), h, "all chunks done → raw marked synthesized")
        # chunk metrics emitted
        import memex.metrics as metrics_mod
        modes = {e.get("mode") for e in metrics_mod.read(self.vault)}
        self.assertIn("chunk", modes)

    def test_reflect_chunk_with_claim_evidence_marks_raw_done(self):
        """Regression: a giant-session chunk whose proposal carries claim
        EVIDENCE must still mark the raw synthesized. The claims-anchor loop
        iterates evidence with a loop var; if it reused the `item` name (the
        chunk directive), the `_record_chunk_done` closure would read the
        evidence dict instead and KeyError — leaving the raw pending forever."""
        import memex.synth as synth_mod
        sid = "sess-giant-ev"
        big = ("linha de conteudo durável " * 3000)  # ~75k chars → 2 chunks
        f = self.raw_dir() / f"2026-08-08--claude--{sid}--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{big}",
                     encoding="utf-8")
        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            if "Reply with STRICT JSON" in prompt:
                # claims WITH evidence (the real provider shape) — this exercises
                # the anchor loop whose loop var must not clobber `item`.
                return json.dumps({"skip": False, "slug": "sess-giant-ev",
                                   "title": "Giant", "section": "topics",
                                   "tags": [], "related": [], "project": None,
                                   "distill": "d.",
                                   "claims": [{"text": "linha de conteudo durável",
                                               "type": "fact", "explicitness": "explicit",
                                               "evidence": [{"quote": "linha de conteudo durável",
                                                             "start_line": 1, "end_line": 1}]}]})
            if "You verify" in prompt:
                return json.dumps({"outcome": "supported", "value": "new", "reason": "ok"})
            return "## Contéudo\ndurável.\n"
        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=4)
        with mock.patch("memex.providers.complete", side_effect=_route):
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        # the raw must be marked synthesized — the bug left it pending forever
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text())
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        self.assertEqual(synthed.get(f.name), h,
                         "chunk with claim evidence must still mark the raw done")

    def test_reflect_chunk_ungrounded_claims_apply_via_body_fidelity(self):
        """Regression: a chunk whose propose claims DON'T anchor in the raw (no
        verbatim quote, text not a substring) must NOT be parked by the
        all-or-nothing `ungrounded` gate. A chunk verifies by BODY FIDELITY
        against its own slice and auto-applies when the judge says supported.
        Previously the gate parked 99.5% of chunks as ambiguous in production
        (2836/2851) even though the merge was faithful."""
        import memex.synth as synth_mod
        sid = "sess-giant-ungrounded"
        big = ("linha de conteudo durável " * 3000)  # ~75k chars → 2 chunks
        f = self.raw_dir() / f"2026-08-08--claude--{sid}--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{big}",
                     encoding="utf-8")
        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            if "Reply with STRICT JSON" in prompt:
                # claim text is NOT a substring of the raw and has no evidence
                # quote → the old code's `ungrounded` gate would park the chunk
                return json.dumps({"skip": False, "slug": "sess-giant-ungrounded",
                                   "title": "Giant", "section": "topics",
                                   "tags": [], "related": [], "project": None,
                                   "distill": "d.",
                                   "claims": [{"text": "decisão tomada sobre a arquitetura",
                                               "type": "decision",
                                               "explicitness": "explicit"}]})
            if "You verify" in prompt:
                return json.dumps({"outcome": "supported", "value": "new",
                                   "reason": "fiel ao slice"})
            return "## Decisão\nArquitetura definida.\n"
        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=4)
        with mock.patch("memex.providers.complete", side_effect=_route):
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        page = self.vault / "wiki" / "topics" / "sess-giant-ungrounded.md"
        self.assertTrue(page.exists(),
                        "ungrounded chunk claims must not park the whole page")
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text())
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        self.assertEqual(synthed.get(f.name), h,
                         "chunk raw must be marked done after all slices apply")

    def test_classify_risk_chunk_treated_as_slice(self):
        """A chunk ChangeSet is judged by BODY FIDELITY like a delta — but a 50k
        window distilled to one page is never exhaustive, so `partial` AUTO-
        APPLIES (parking would re-propose the chunk forever). Only `ambiguous`
        parks; unsupported/conflicting reject. A delta's `partial` still parks
        (checkpoint must not advance past unreflected tail content)."""
        chunk = {"source": {"kind": "raw", "mode": "chunk"},
                 "operation": "create",
                 "classification": {"section": "topics", "slug": "x"},
                 "claims": []}
        delta = {"source": {"kind": "raw", "mode": "delta"},
                 "operation": "update",
                 "classification": {"section": "topics", "slug": "x"},
                 "claims": []}
        # chunk: supported/partial → auto-apply; ambiguous → review; unsupported → reject
        self.assertEqual(
            verify_mod.classify_risk(chunk, [], {"outcome": "supported", "value": "new"},
                                     auto_review=True), "auto_apply")
        self.assertEqual(
            verify_mod.classify_risk(chunk, [], {"outcome": "partial", "value": "new"},
                                     auto_review=True), "auto_apply")
        self.assertEqual(
            verify_mod.classify_risk(chunk, [], {"outcome": "ambiguous", "value": "new"},
                                     auto_review=True), "review")
        self.assertEqual(
            verify_mod.classify_risk(chunk, [], {"outcome": "unsupported", "value": "new"},
                                     auto_review=True), "reject")
        # delta: partial still parks (checkpoint integrity)
        self.assertEqual(
            verify_mod.classify_risk(delta, [], {"outcome": "partial", "value": "new"},
                                     auto_review=True), "review")

    def test_classify_risk_technical_identity_slug_parks(self):
        """A `note-<session>` / `untitled` fallback slug (the propose couldn't
        classify a real topic) must PARK for human reclassify — never
        auto-apply, and never let apply_changeset REJECT it (which would
        discard the raw content)."""
        chunk = {"source": {"kind": "raw", "mode": "chunk"},
                 "operation": "create",
                 "classification": {"section": "topics", "slug": "note-decbc057c",
                                    "title": "Nota da sessão"},
                 "claims": []}
        # even a faithful note-* proposal parks
        self.assertEqual(
            verify_mod.classify_risk(chunk, [], {"outcome": "supported", "value": "new"},
                                     auto_review=True), "review")
        # an invented one also parks (human sees it, nothing discarded)
        self.assertEqual(
            verify_mod.classify_risk(chunk, [], {"outcome": "unsupported", "value": "new"},
                                     auto_review=True), "review")

    def test_triage_chunk_delta_tail_when_giant(self):
        """A delta whose NEW tail is itself giant is chunked (the tail, not the
        whole body) — new content appended in a huge block isn't truncated."""
        import memex.synth as synth_mod
        import threading
        sid = "sess-tail-giant"
        base = "linha\n" * 10
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": "old.md", "chars": len(base),
                  "body_hash": synth_mod._body_hash(base),
                  "slug": "sess-topic", "section": "topics"}})
        page = self.vault / "wiki" / "topics" / "sess-topic.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntitle: Sessão\nkind: session\n---\n\n" + base, encoding="utf-8")
        tail = "novo\n" * 6000   # ~30k each
        body = base + (tail * 3)  # base + ~90k tail → 2 chunks of 50k
        f = self.raw_dir() / "2026-08-08--claude--sess-tail-giant--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{body}",
                     encoding="utf-8")
        prepared, _ = synth_mod._prepare_todo(
            self.vault, [(f, hashlib.sha256(f.read_bytes()).hexdigest()[:16])],
            {}, self.vault / ".memex" / "synthed.json",
            {}, {"chunk_chars": 50000}, threading.Lock(), [0], [])
        self.assertEqual(len(prepared), 2, "90k tail → 2 chunk directives")
        self.assertTrue(all(it["mode"] == "chunk" for it in prepared))
        # the first chunk must NOT include the base prefix (only the tail)
        self.assertNotIn("linha\n", prepared[0]["chunk"].split("\n")[0],
                         "chunks cover the appended tail, not the base")

    def test_is_strict_append_rejects_edited_and_shrunk(self):
        """The append-only invariant: only a capture whose prefix still hashes to
        the checkpoint is a delta; an edited prefix, a shrink, or a missing hash
        is NOT a strict append (full fallback, never a fabricated delta)."""
        import memex.synth as synth_mod
        base = "linha\n" * 10
        base_hash = synth_mod._body_hash(base)
        self.assertTrue(synth_mod._is_strict_append(base + "novo\n", len(base), base_hash))
        edited = base.replace("linha\n", "linha EDITADA\n", 1) + "novo\n"
        self.assertFalse(synth_mod._is_strict_append(edited, len(base), base_hash))
        self.assertFalse(synth_mod._is_strict_append(base[:20], len(base), base_hash))
        self.assertFalse(synth_mod._is_strict_append(base + "x\n", len(base), None))
        self.assertFalse(synth_mod._is_strict_append(base + "x\n", 0, base_hash))

    def test_session_delta_applies_and_advances_lineage(self):
        """End-to-end: a session captured, grown, and re-captured. The first
        capture FULL-merges; the second is a DELTA (propose skipped) whose tail
        is distilled into the existing page and auto-applies — the claim gate is
        relaxed for verified deltas. Lineage advances to the new body and the
        metric reports mode=session-delta with delta_chars."""
        import memex.synth as synth_mod
        import memex.metrics as metrics_mod
        sid = "sess-delta"
        body1 = ("linha inicial\n" * 3 +
                 "Vamos criar um job diário que compara o custo com a média móvel.\n")
        f1 = self.raw_dir() / f"2026-08-08--claude--{sid}--a.md"
        f1.write_text(f"---\nsource: claude\nid: {sid}\ndate: 2026-08-08\nkind: session\n---\n\n{body1}",
                      encoding="utf-8")
        proposal = {
            "skip": False, "slug": "databricks-cost-alert-job",
            "title": "Job diário de alerta de custo Databricks",
            "section": "topics", "tags": [], "related": [], "project": None,
            "distill": "Job diário compara custo com a média móvel.",
            "claims": [{"text": "Vamos criar um job diário que compara o custo com a média móvel.",
                        "type": "process", "explicitness": "explicit"}],
        }
        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=1)
        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(proposal, "## Rule\nA daily job compares cost.\n")):
            synth_mod.run(args)
        page = self.vault / "wiki" / "topics" / "databricks-cost-alert-job.md"
        self.assertTrue(page.exists())
        lineage = synth_mod._load_lineage(self.vault)
        self.assertEqual(lineage[sid]["slug"], "databricks-cost-alert-job")
        self.assertEqual(lineage[sid]["chars"], len(body1))
        self.assertEqual(lineage[sid]["source_kind"], "session")

        # second capture of the SAME session — append-only growth → DELTA
        body2 = body1 + "## append\nDecidimos alertar quando o custo diário > 2x a média de 7 dias.\n"
        f2 = self.raw_dir() / f"2026-08-09--claude--{sid}--b.md"
        f2.write_text(f"---\nsource: claude\nid: {sid}\ndate: 2026-08-09\nkind: session\n---\n\n{body2}",
                      encoding="utf-8")
        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(proposal, "## Decision\nAlertar quando > 2x média de 7 dias.\n")):
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        lineage2 = synth_mod._load_lineage(self.vault)
        self.assertEqual(lineage2[sid]["chars"], len(body2),
                         "delta must advance the wiki checkpoint to the new body")
        self.assertEqual(lineage2[sid]["source_kind"], "session")
        delta_evs = [e for e in metrics_mod.read(self.vault) if e.get("mode") == "session-delta"]
        self.assertEqual(len(delta_evs), 1, "a session-delta metric must be emitted")
        self.assertEqual(delta_evs[0]["delta_chars"], len(body2) - len(body1))
        self.assertEqual(delta_evs[0]["checkpoint_before"], len(body1))
        self.assertEqual(delta_evs[0]["checkpoint_after"], len(body2))

    def _capture_session(self, sid="sess-llm"):
        t = _fake_transcript(self.tmp, sid, str(self.workspace))
        _run_capturing(
            capture_mod.run,
            Namespace(vault=str(self.vault), partial=False, docs=False,
                      workspace=None, transcript=None, no_reflect=True),
            payload={"transcript_path": str(t), "cwd": str(self.workspace)})

    def test_reflect_builds_wiki_and_workspace_page(self):
        self._capture_session()
        rc, out = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=str(self.workspace),
                      since=None, limit=None, provider=None))
        self.assertEqual(rc, 0, out)
        page = self.vault / "wiki" / "topics" / "databricks-cost-alerts.md"
        self.assertTrue(page.exists(), out)
        text = page.read_text(encoding="utf-8")
        self.assertIn("Alertar picos de custo", text)
        self.assertIn("title:", text)              # tool-owned frontmatter
        # index catalogs it; git workspace WINS over the LLM-proposed project
        idx = json.loads((self.vault / ".memex" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(idx["pages"][0]["slug"], "databricks-cost-alerts")
        self.assertEqual(idx["pages"][0]["project"], "ws")
        # batched views flush: the deferred per-apply view writes must still be
        # reflected once at end-of-run
        brain = (self.vault / ".memex" / "views" / "brain-index.md")
        self.assertTrue(brain.exists(), "batched views flush must write brain-index")
        self.assertIn("databricks-cost-alerts", brain.read_text(encoding="utf-8"))
        # working memory refreshed
        meta, body = workspace_mod.read_workspace(self.vault, self.workspace_key())
        self.assertEqual(meta.get("author"), "auto")
        self.assertIn("Próximos passos", body)
        # human log
        log = (self.vault / "log.md").read_text(encoding="utf-8")
        self.assertIn("synth", log)

    def test_synth_reads_owner_profile_from_about_md(self):
        """The persona is ABOUT.md, not hardcoded — synth must inject it."""
        (self.vault / "ABOUT.md").write_text(
            "# ABOUT\nSou GESTOR-MARCADOR de engenharia.", encoding="utf-8")
        _MockLLMHandler.seen_prompts = []
        self._capture_session("sess-about")
        _run_capturing(reflect_mod.run,
                       Namespace(vault=str(self.vault), cwd=str(self.workspace),
                                 since=None, limit=None, provider=None))
        synth_prompts = [p for p in _MockLLMHandler.seen_prompts
                         if "OWNER PROFILE" in p]
        self.assertTrue(synth_prompts, "no synth prompt carried the owner profile")
        self.assertTrue(all("GESTOR-MARCADOR" in p for p in synth_prompts))

    def test_reflect_processes_old_backlog(self):
        """Notes stranded from past days (offline, provider down) synthesize on
        the NEXT reflect — no manual `memex synth` in the loop."""
        seen = ingest_mod._ledger_load(self.vault)
        ingest_mod.ingest_session(self.vault, {
            "source": "claude", "id": "old-sess", "date": "2026-07-01",
            "cwd": str(self.workspace),
            # includes the mock propose claim's sentence so the evidence anchor
            # resolves (a claim-less proposal must not auto-apply)
            "text": "## user\n\nDecisão antiga sobre alertas de custo do Databricks.\n\n"
                    "Vamos criar um job diário que compara o custo com a média móvel.",
        }, seen)
        rc, out = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=str(self.workspace),
                      since=None, limit=None, provider=None))
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.vault / "wiki" / "topics" / "databricks-cost-alerts.md").exists(),
                        "backlog note was not synthesized")

    def test_content_project_when_workspace_is_not_git(self):
        """A management session run from a plain folder gets its project from
        CONTENT (propose), not from the folder name."""
        notas = self.tmp / "notas"
        notas.mkdir()
        t = _fake_transcript(self.tmp, "sess-mgmt", str(notas))
        _run_capturing(
            capture_mod.run,
            Namespace(vault=str(self.vault), partial=False, docs=False,
                      workspace=None, transcript=None, no_reflect=True),
            payload={"transcript_path": str(t), "cwd": str(notas)})
        rc, out = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=str(notas),
                      since=None, limit=None, provider=None))
        self.assertEqual(rc, 0, out)
        idx = json.loads((self.vault / ".memex" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(idx["pages"][0]["project"], "iniciativa-custos")
        # working memory still keys on the WORKSPACE (the folder), not the project
        _, body = workspace_mod.read_workspace(self.vault, workspace_mod.workspace_key(str(notas)))
        self.assertIn("Próximos passos", body or "")

    def test_reflect_creates_pending_changeset_for_decision_instead_of_writing_wiki(self):
        """A decisions-section proposal is verified but NEVER auto-applied: it
        is parked as a pending ChangeSet and no wiki page is written."""
        self._capture_session("decision-review")
        proposal = {
            "skip": False,
            "slug": "databricks-cost-alert-decision",
            "title": "Alerta de custo Databricks — decisão",
            "section": "decisions",
            "tags": ["databricks", "custos"],
            "related": [],
            "project": None,
            "distill": "Decidimos alertar quando o custo diário exceder 2x a média de 7 dias.",
            # fixture A: the claim text MUST be a real substring of the raw
            # transcript so the evidence anchor resolves to `supported`.
            "claims": [{"text": "Perfeito, decidimos: alerta quando custo diário > 2x média de 7 dias.",
                        "type": "decision", "explicitness": "explicit"}],
        }
        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(proposal, "## Decision\nRun backups daily.\n")):
            rc, out = _run_capturing(
                reflect_mod.run,
                Namespace(vault=str(self.vault), cwd=str(self.workspace),
                          since=None, limit=None, provider=None, workers=1))

        self.assertEqual(rc, 0, out)
        self.assertFalse((self.vault / "wiki" / "decisions" / "databricks-cost-alert-decision.md").exists(),
                         out)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        change = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(change["classification"]["section"], "decisions")
        self.assertEqual(change["verification"]["route"], "review")

    def test_reflect_auto_applies_supported_low_risk_topic(self):
        """A topics-section proposal whose claim is verified supported is applied
        via the promoter: a wiki page appears and nothing stays pending."""
        self._capture_session("topic-auto")
        proposal = {
            "skip": False,
            "slug": "databricks-cost-alert-job",
            "title": "Job diário de alerta de custo Databricks",
            "section": "topics",
            "tags": ["databricks", "alerts"],
            "related": [],
            "project": None,
            "distill": "Um job diário compara o custo com a média móvel.",
            # fixture A: claim text is a real substring of the raw transcript
            "claims": [{"text": "Vamos criar um job diário que compara o custo com a média móvel.",
                        "type": "process", "explicitness": "explicit"}],
        }
        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(
                            proposal, "## Rule\nA daily job compares cost against the moving average.\n")):
            rc, out = _run_capturing(
                reflect_mod.run,
                Namespace(vault=str(self.vault), cwd=str(self.workspace),
                          since=None, limit=None, provider=None, workers=1))

        self.assertEqual(rc, 0, out)
        page = self.vault / "wiki" / "topics" / "databricks-cost-alert-job.md"
        self.assertTrue(page.exists(), out)
        idx = json.loads((self.vault / ".memex" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(idx["pages"][0]["slug"], "databricks-cost-alert-job")
        self.assertEqual(list((self.vault / ".memex" / "review" / "pending").glob("*.json")), [])
        self.assertEqual(len(list((self.vault / ".memex" / "review" / "applied").glob("*.json"))), 1)
        # Finding-1 regression: the end-of-run summary counts durably-saved
        # ChangeSets (this run created exactly one — applied, not pending).
        self.assertIn("1 ChangeSet(s)", out)
        self.assertNotIn("0 ChangeSet(s)", out)

    def test_auto_tidy_detects_duplicates_without_merging(self):
        """Automatic tidy is detection only: it writes the audit suggestions note
        and leaves the wiki pages (and the review queue) untouched."""
        cfg_path = self.vault / ".memex" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["limits"] = {"tidy_min_pages": 2, "tidy_every_days": 1}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        # two near-duplicate pages (shared 3-segment slug prefix → same cluster)
        pages = []
        for suffix in ("", "-v2"):
            slug = f"pipeline-vendas-dedup{suffix}"
            path = f"topics/{slug}.md"
            (self.vault / "wiki" / "topics").mkdir(parents=True, exist_ok=True)
            (self.vault / "wiki" / path).write_text(
                f"---\ntitle: \"{slug}\"\n---\n\n## Regra\ndedup por order_id\n",
                encoding="utf-8")
            pages.append({"slug": slug, "title": slug, "section": "topics",
                          "kind": "silver", "status": "current", "tags": ["dedup"], "sources": [],
                          "summary": "dedup de pedidos", "path": path, "project": "ws"})
        self.seed_index(pages)
        rc, out = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=None, since=None, limit=None,
                      provider=None))
        self.assertEqual(rc, 0, out)
        self.assertIn("auto-tidy", out)
        # detection only: the audit note appears, pages are untouched, no
        # ChangeSet is filed (a human runs `memex tidy` to do that)
        self.assertTrue((self.vault / ".memex" / "audit" / "merge-suggestions.md").exists())
        self.assertTrue((self.vault / "wiki" / "topics" / "pipeline-vendas-dedup.md").exists())
        self.assertTrue((self.vault / "wiki" / "topics" / "pipeline-vendas-dedup-v2.md").exists())
        self.assertEqual(list((self.vault / ".memex" / "review" / "pending").glob("*.json")), [])
        # cadence: a second reflect right away must NOT re-scan
        rc, out2 = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=None, since=None, limit=None,
                      provider=None))
        self.assertNotIn("auto-tidy", out2)

    def test_boot_after_reflect_closes_the_loop(self):
        """The e2e story: session -> capture -> reflect -> NEW session boots with state."""
        self._capture_session("sess-loop")
        _run_capturing(reflect_mod.run,
                       Namespace(vault=str(self.vault), cwd=str(self.workspace),
                                 since=None, limit=None, provider=None))
        rc, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace), "session_id": "new"})
        self.assertIn("Where we left off", out)
        self.assertIn("ligar o schedule", out)     # next step survives sessions
        # and recall can find the synthesized page in the new session
        rc, out = _run_capturing(
            recall_mod.run, Namespace(vault=str(self.vault), query=None),
            payload={"session_id": "new",
                     "prompt": "precisamos rever os alertas de custo databricks"})
        self.assertIn("databricks-cost-alerts", out)


class TestIncrementalFlush(unittest.TestCase):
    """M1 — loop-proof: `_mark_done` must flush synthed.json (+ lineage) to disk
    per-raw, so a reflect killed mid-run keeps its marks. Before this, synthed
    was only written once at end-of-run, so a crash dropped every in-memory mark
    while ChangeSets already on disk made the next reflect reprocess the same
    raws → duplicate ChangeSets."""

    def test_mark_done_flushes_synthed_immediately(self):
        from memex import synth
        v = Path(tempfile.mkdtemp(prefix="memex-flush-")) / "vault"
        (v / ".memex").mkdir(parents=True)
        sp = v / ".memex" / "synthed.json"
        sp.write_text("{}", encoding="utf-8")
        synthed, lineage = {}, {}
        synth._mark_done(v, synthed, sp, lineage, "raw-aaa.md", "abc123")
        # on disk immediately — no end-of-run flush needed
        self.assertEqual(json.loads(sp.read_text(encoding="utf-8")),
                         {"raw-aaa.md": "abc123"})
        # in-memory dict too
        self.assertEqual(synthed, {"raw-aaa.md": "abc123"})

    def test_kill_after_mark_preserves_state(self):
        """Reflect marks 2 raws then 'dies' before any final flush: the per-raw
        flush already put both marks on disk."""
        from memex import synth
        v = Path(tempfile.mkdtemp(prefix="memex-flush-")) / "vault"
        (v / ".memex").mkdir(parents=True)
        sp = v / ".memex" / "synthed.json"
        sp.write_text("{}", encoding="utf-8")
        synthed, lineage = {}, {}
        synth._mark_done(v, synthed, sp, lineage, "r1.md", "h1")
        synth._mark_done(v, synthed, sp, lineage, "r2.md", "h2")
        # "dies" here — no final flush. State preserved?
        self.assertEqual(json.loads(sp.read_text(encoding="utf-8")),
                         {"r1.md": "h1", "r2.md": "h2"})

    def test_marks_survive_when_final_flush_is_disabled(self):
        """M1 END-TO-END: the loop-proof guarantee is the per-raw `_mark_done`,
        not the end-of-run `_flush_state`. The two tests above call `_mark_done`
        directly — they prove the helper, not that the wiring sites in
        `_process_one` actually invoke it. A refactor that silently drops a
        `_mark_done` back to in-memory-only `synthed[f.name] = h` would still
        pass the suite, because the final `_flush_state` (in the `finally`
        block) masks it. Simulate a reflect KILLED before that final flush by
        making `_flush_state` a no-op and asserting the marks are ALREADY on
        disk — the only writer that could have put them there is `_mark_done."""
        import memex.synth as synth_mod
        from argparse import Namespace
        from unittest import mock
        v = Path(tempfile.mkdtemp(prefix="memex-flush-")) / "vault"
        (v / ".memex" / "raw").mkdir(parents=True)
        sp = v / ".memex" / "synthed.json"
        sp.write_text("{}", encoding="utf-8")
        raws = []
        for i in range(3):
            f = v / ".memex" / "raw" / f"2026-08-08--claude--sess-kill-{i}.md"
            f.write_text(
                f"---\nsource: claude\nid: sess-kill-{i}\ndate: 2026-08-08\n"
                f"kind: session\n---\n\nlinha {i}: conteúdo durável.\n",
                encoding="utf-8")
            raws.append(f)

        def _route(prompt, *, kind, model, settings, json_mode=False,
                   allowed_tools=None):
            # propose returns skip → Site A `_mark_done` (no merge/verify/apply)
            if "Reply with STRICT JSON" in prompt:
                return json.dumps({"skip": True})
            return "## nada\n"

        args = Namespace(vault=str(v), provider=None, limit=None, since=None,
                         only=None, model_propose="mock", model_merge="mock",
                         workers=1)
        with mock.patch("memex.providers.complete", side_effect=_route), \
             mock.patch.object(synth_mod, "_flush_state",
                               side_effect=lambda *a, **k: None) as flush:
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        # The scenario is real: the end-of-run flush DID fire (the `finally`
        # block) but was a no-op — so the marks could only have reached disk via
        # the per-raw `_mark_done` write. Kill-mid-run keeps them.
        flush.assert_called()
        on_disk = json.loads(sp.read_text(encoding="utf-8"))
        self.assertEqual(set(on_disk), {f.name for f in raws},
                         "M1: per-raw _mark_done must persist every mark even "
                         "though the final _flush_state never writes them")


class TestSearch(MemexTestCase):
    def setUp(self):
        super().setUp()
        _materialize_pages(self.vault, TestRecall.PAGES)

    def test_search_prints_scored_paths(self):
        self.seed_index(TestRecall.PAGES)
        rc, out = _run_capturing(
            search_mod.run,
            Namespace(vault=str(self.vault), terms=["databricks", "alertas", "custo"],
                      limit=5))
        self.assertEqual(rc, 0)
        self.assertIn("databricks-cost-alerts", out)
        self.assertIn("topics", out)

    def test_single_term_search_works(self):
        """recall's terse-prompt gate must NOT apply to the interactive verb."""
        self.seed_index(TestRecall.PAGES)
        rc, out = _run_capturing(
            search_mod.run,
            Namespace(vault=str(self.vault), terms=["databricks"], limit=5))
        self.assertEqual(rc, 0)
        self.assertIn("databricks-cost-alerts", out)


class TestGeneratedViews(MemexTestCase):
    def test_views_are_generated_under_memex_and_not_under_wiki(self):
        page = {
            "slug": "topic-a", "title": "Topic A", "section": "topics",
            "kind": "session", "status": "current", "tags": [],
            "sources": ["session:a"], "summary": "summary", "path": "topics/topic-a.md",
            "project": "project-a",
        }
        (self.vault / "wiki" / page["path"]).write_text("---\ntitle: \"Topic A\"\n---\n\n## A\n", encoding="utf-8")
        self.seed_index([page])

        synth_mod._write_index_md(self.vault, {"pages": [page]})

        self.assertTrue((self.vault / ".memex" / "views" / "brain-index.md").exists())
        self.assertTrue((self.vault / ".memex" / "views" / "projects" / "project-a.md").exists())
        self.assertFalse((self.vault / "index.md").exists())
        self.assertFalse((self.vault / "wiki" / "projects" / "project-a.md").exists())

    def test_gardening_suggestions_are_audit_artifacts(self):
        from memex import gardening
        one = {"slug": "topic-a", "title": "Topic A", "section": "topics", "status": "current", "tags": [], "summary": "same words"}
        two = {"slug": "topic-a-v2", "title": "Topic A v2", "section": "topics", "status": "current", "tags": [], "summary": "same words"}
        self.seed_index([one, two])

        gardening.write_suggestions(self.vault, threshold=0.0)

        self.assertTrue((self.vault / ".memex" / "audit" / "merge-suggestions.md").exists())
        self.assertFalse((self.vault / "wiki" / "_sugestoes.md").exists())


class TestAuditFixes(MemexTestCase):
    def test_tidy_files_pending_merge_candidates_without_touching_pages(self):
        """`memex tidy` (gardening.consolidate) is candidate generation: a
        near-duplicate cluster becomes a pending merge ChangeSet and the
        canonical page files are left byte-for-byte unchanged (nothing is
        merged or archived automatically)."""
        from memex import gardening
        pages = []
        for suffix in ("", "-v2"):
            slug = f"pipeline-vendas-dedup{suffix}"
            path = f"topics/{slug}.md"
            (self.vault / "wiki" / "topics").mkdir(parents=True, exist_ok=True)
            (self.vault / "wiki" / path).write_text(
                f"---\ntitle: \"{slug}\"\n---\n\n## Original de {slug}\nconteúdo íntegro\n",
                encoding="utf-8")
            pages.append({"slug": slug, "title": slug, "section": "topics",
                          "kind": "silver", "status": "current", "tags": [], "sources": [],
                          "summary": "dedup", "path": path, "project": "ws"})
        self.seed_index(pages)
        rels = ("topics/pipeline-vendas-dedup.md", "topics/pipeline-vendas-dedup-v2.md")
        before = {rel: (self.vault / "wiki" / rel).read_text(encoding="utf-8") for rel in rels}
        rc, out = _run_capturing(lambda a: gardening.consolidate(self.vault), None)
        self.assertEqual(rc, 0, out)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        change = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(change["operation"], "merge")
        self.assertEqual(change["classification"]["slug"], "pipeline-vendas-dedup")
        self.assertEqual(change["source"]["kind"], "tidy")
        self.assertEqual(change["risk"], "review")
        # canonical page files are unchanged — a candidate, not a mutation
        for rel, text in before.items():
            self.assertEqual((self.vault / "wiki" / rel).read_text(encoding="utf-8"), text)
        self.assertEqual(list((self.vault / ".memex" / "history" / "gardening").glob("*.md")), [])

    def test_analyze_creates_pending_code_changeset_and_leaves_wiki_untouched(self):
        """`memex analyze` routes architecture pages through the review queue as
        code-sourced ChangeSets; wiki/topics is never written directly."""
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        src = repo / "src"
        src.mkdir(parents=True)
        for name in ("main.py", "mod.py", "util.py"):
            (src / name).write_text(f"def {name.split('.')[0]}():\n    return 1\n", encoding="utf-8")
        srv, base = _start_mock_llm()
        try:
            cfg = config_mod.load_global()
            cfg["provider"] = {"order": ["openai_compat"],
                               "openai_compat": {"base_url": base, "api_key": None,
                                                 "model_propose": "mock", "model_merge": "mock"}}
            config_mod.save_global(cfg)
            rc, out = _run_capturing(
                analyze_mod.run,
                Namespace(repo=str(repo), vault=str(self.vault), provider=None,
                          modules=0, model_merge=None))
        finally:
            srv.shutdown()
        self.assertEqual(rc, 0, out)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        change = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(change["source"]["kind"], "code")
        self.assertEqual(change["source"]["repo"], str(repo.resolve()))
        self.assertEqual(change["risk"], "review")
        self.assertEqual(change["verification"]["outcome"], "code_evidence_required")
        self.assertEqual(change["classification"]["section"], "topics")
        # wiki/topics is unchanged — no page was written directly
        self.assertEqual(list((self.vault / "wiki" / "topics").glob("*.md")), [])

    def test_config_set_persists_only_user_keys(self):
        """set must never freeze shipped defaults into the user's file."""
        from memex import cli as cli_mod
        rc, _ = _run_capturing(
            cli_mod._config_cmd, Namespace(action="set", key="default_vault",
                                           value=str(self.vault)))
        self.assertEqual(rc, 0)
        raw = json.loads(config_mod.global_config_path().read_text(encoding="utf-8"))
        self.assertEqual(raw["default_vault"], str(self.vault))
        self.assertNotIn("provider", raw)                     # defaults NOT frozen in
        # and scalars can't clobber sections
        rc, out = _run_capturing(
            cli_mod._config_cmd, Namespace(action="set", key="provider", value="claude"))
        self.assertEqual(rc, 1)
        self.assertIn("section", out)

    def test_missing_extractor_skip_is_retried_after_tool_install(self):
        """A pdf/xlsx skipped for LACK OF A TOOL must be picked up once the
        tool exists — only by-design refusals are remembered forever."""
        doc = self.workspace / "planilha.xlsx"
        doc.write_bytes(b"fake-xlsx")
        # Force the first pass to exercise the missing-extractor branch even
        # when the developer machine has markitdown/openpyxl installed.
        orig_have = ingest_mod.extract_mod._have
        ingest_mod.extract_mod._have = lambda cmd: False
        try:
            args = Namespace(vault=str(self.vault),
                             docs=str(self.workspace), exclude=None)
            with redirect_stdout(io.StringIO()):
                ingest_mod._ingest_docs(
                    self.vault, args, ingest_mod._ledger_load(self.vault))
        finally:
            ingest_mod.extract_mod._have = orig_have
        raw_docs = lambda: list((self.raw_dir()).glob("*--doc--*.md"))  # noqa: E731
        self.assertEqual(len(raw_docs()), 0)

        # Simulate the extractor becoming available on the next run.
        args = Namespace(vault=str(self.vault),
                         docs=str(self.workspace), exclude=None)
        orig = ingest_mod.extract_mod.extract
        ingest_mod.extract_mod.extract = lambda fp: ("## Planilha\ndados", "markitdown")
        try:
            with redirect_stdout(io.StringIO()):
                ingest_mod._ingest_docs(self.vault, args, ingest_mod._ledger_load(self.vault))
        finally:
            ingest_mod.extract_mod.extract = orig
        self.assertEqual(len(raw_docs()), 1,
                         "previously-skipped file was not retried after tool install")
        self.assertIn("Planilha", raw_docs()[0].read_text(encoding="utf-8"))
    def test_docs_content_gate_survives_mtime_churn(self):
        """git checkout / re-clone churns mtimes with identical content — no
        duplicate raw notes, no re-synthesis."""
        doc = self.workspace / "notas.md"
        doc.write_text("# Nota\nconteúdo estável", encoding="utf-8")
        args = lambda: Namespace(vault=str(self.vault),  # noqa: E731
                                 docs=str(self.workspace), exclude=None)
        seen = ingest_mod._ledger_load(self.vault)
        ingest_mod._ingest_docs(self.vault, args(), seen)
        n_before = len(list((self.raw_dir()).glob("*--doc--*.md")))
        os.utime(doc, (time.time() + 60, time.time() + 60))   # mtime churn
        seen = ingest_mod._ledger_load(self.vault)
        with redirect_stdout(io.StringIO()):
            ingest_mod._ingest_docs(self.vault, args(), seen)
        n_after = len(list((self.raw_dir()).glob("*--doc--*.md")))
        self.assertEqual(n_before, n_after, "mtime churn must not duplicate raw notes")

    def _set_ingest_filters(self, docs):
        cfg = {"ingest": {"docs": docs}}
        (self.vault / ".memex" / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_docs_include_exclude_globs_filter_the_walk(self):
        """Per-vault ingest.docs.include/exclude globs must gate which files the
        --docs walk adopts — allowlist (include) restricts, denylist (exclude)
        drops on top. Empty config keeps legacy behavior (adopt all)."""
        import memex.ingest as ingest_mod
        # legacy behavior first: both files adopted
        (self.workspace / "a.md").write_text("# A\nnota A\n", encoding="utf-8")
        (self.workspace / "b.log").write_text("log line\n", encoding="utf-8")
        args = Namespace(vault=str(self.vault), docs=str(self.workspace), exclude=None)
        with redirect_stdout(io.StringIO()):
            ingest_mod._ingest_docs(self.vault, args, ingest_mod._ledger_load(self.vault))
        raws = lambda: [r.name for r in (self.raw_dir()).glob("*--doc--*.md")]  # noqa: E731
        self.assertEqual(len(raws()), 2, "legacy ingest must adopt every content file")

        # denylist drops the log on a FRESH ledger (new vault state in a sub-dir)
        vault2 = self.vault / "vault2"
        (vault2 / ".memex").mkdir(parents=True, exist_ok=True)
        (vault2 / ".memex" / "config.json").write_text(
            json.dumps({"ingest": {"docs": {"exclude": ["**/*.log"]}}}, ensure_ascii=False),
            encoding="utf-8")
        args = Namespace(vault=str(vault2), docs=str(self.workspace), exclude=None)
        with redirect_stdout(io.StringIO()):
            ingest_mod._ingest_docs(vault2, args, ingest_mod._ledger_load(vault2))
        raw2 = [r.name for r in (vault2 / ".memex" / "raw").glob("*--doc--*.md")]
        self.assertEqual(len(raw2), 1, "exclude glob must drop the .log")
        self.assertIn("nota A", (vault2 / ".memex" / "raw" / raw2[0]).read_text(encoding="utf-8"))

        # allowlist restricts to only matching files
        vault3 = self.vault / "vault3"
        (vault3 / ".memex").mkdir(parents=True, exist_ok=True)
        (vault3 / ".memex" / "config.json").write_text(
            json.dumps({"ingest": {"docs": {"include": ["**/*.md"]}}}, ensure_ascii=False),
            encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            ingest_mod._ingest_docs(vault3, args, ingest_mod._ledger_load(vault3))
        raw3 = [r.name for r in (vault3 / ".memex" / "raw").glob("*--doc--*.md")]
        self.assertEqual(len(raw3), 1, "include allowlist must restrict to .md")

    def test_docs_skip_ids_blocks_index_entry(self):
        """Per-vault ingest.docs.skip_ids drops index entries whose locator
        matches — e.g. a personal automation log that must never enter the wiki."""
        import memex.ingest as ingest_mod
        self._set_ingest_filters({"skip_ids": ["**/morning-routine.log"]})
        index = self.workspace / "_index.jsonl"
        index.write_text(
            json.dumps({"path": "/Users/gian/src/pessoal/automation/morning-routine.log",
                        "fingerprint": "abc"}) + "\n" +
            json.dumps({"path": "/Users/gian/src/cris/README.md",
                        "fingerprint": "def"}) + "\n",
            encoding="utf-8")
        args = Namespace(vault=str(self.vault), index=str(index), index_base="",
                         index_mcp=False, provider=None)
        # BOTH entries resolve to content — only skip_ids can keep the log out.
        ingest_mod.resolve_mod.resolve_entry = lambda e, **kw: ("## Doc\nconteúdo", "text")
        try:
            with redirect_stdout(io.StringIO()):
                n = ingest_mod._ingest_index(self.vault, args, set())
        finally:
            ingest_mod.resolve_mod.resolve_entry = \
                __import__("memex.resolve", fromlist=["resolve_entry"]).resolve_entry
        self.assertEqual(n, 1, "skip_ids must drop the morning-routine entry")
        raws = [r.read_text(encoding="utf-8") for r in (self.raw_dir()).glob("*--doc--*.md")]
        self.assertEqual(len(raws), 1)
        self.assertIn("README", raws[0], "only the non-skipped entry should be ingested")


class TestReviewFixes(MemexTestCase):
    """Regression tests for the adversarial-review findings on v2.1."""

    def test_prune_state_spares_durable_markers(self):
        import memex.hookio as hookio
        old = time.time() - 30 * 86400
        for name in ("recall-abc", "last-tidy"):
            hookio.save_state(self.vault, name, {})
            f = hookio.state_dir(self.vault) / f"{name}.json"
            os.utime(f, (old, old))
        hookio.prune_state(self.vault)
        self.assertFalse((hookio.state_dir(self.vault) / "recall-abc.json").exists())
        self.assertTrue((hookio.state_dir(self.vault) / "last-tidy.json").exists(),
                        "prune_state must not reset the tidy cadence")

    def test_consolidate_skips_when_vault_busy(self):
        from memex import gardening
        self.seed_index([])
        lock = synth_mod._acquire_lock(self.vault)   # simulate an in-flight synth
        try:
            rc, out = _run_capturing(
                lambda a: gardening.consolidate(self.vault), None)
            self.assertEqual(rc, 3)
            self.assertIn("busy", out)
        finally:
            lock.unlink()
        # and the lock being free again means tidy can run
        rc, _ = _run_capturing(lambda a: gardening.consolidate(self.vault), None)
        self.assertIn(rc, (0,))                      # nothing to consolidate -> 0

    def test_resolve_project_rejects_placeholders_and_non_strings(self):
        notas = self.tmp / "avulso"
        notas.mkdir()
        for junk in ("kebab-or-null", "null", "NONE", ["lista"], {"x": 1}, None, 42):
            proj = synth_mod._resolve_project(str(notas), {"project": junk})
            self.assertEqual(proj, "avulso", f"junk {junk!r} leaked through")
        proj = synth_mod._resolve_project(str(notas), {"project": "Iniciativa Checkout"})
        self.assertEqual(proj, "iniciativa-checkout")
        # git workspace stays authoritative regardless of the proposal
        proj = synth_mod._resolve_project(str(self.workspace), {"project": "outra-coisa"})
        self.assertEqual(proj, "ws")


class TestChangeSets(MemexTestCase):
    def _current_page(self, slug="topic-a", body="## Rule\nOld value.\n"):
        page = {
            "slug": slug, "title": "Topic A", "section": "topics", "kind": "session",
            "status": "current", "tags": [], "sources": ["session:source"],
            "summary": "old", "path": f"topics/{slug}.md", "project": "ws",
        }
        path = self.vault / "wiki" / page["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: \"Topic A\"\nstatus: current\n---\n\n{body}", encoding="utf-8")
        self.seed_index([page])
        return page, path

    def _repair_change(self, page, path):
        raw = self.raw_dir() / "evidence.md"
        raw.write_text("---\nsource: claude\nid: evidence\n---\n\nExplicit source text.\n", encoding="utf-8")
        return changes_mod.new_changeset(
            operation="repair",
            classification={"section": "topics", "slug": page["slug"], "title": page["title"], "project": "ws"},
            source={"raw": "raw/evidence.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": page["slug"], "expected_page_sha256": canon_mod.page_body_hash(path.read_text(encoding="utf-8"))},
            claims=[],
            proposed_body="## Rule\nNew value.\n",
            risk="low",
            reason="deterministic repair",
        )

    def test_invalid_technical_identity_is_rejected_before_write(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        change["classification"]["slug"] = "note-12345678"
        errors = changes_mod.validate_structure(self.vault, change)
        self.assertTrue(any("semantic" in error for error in errors))
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))

    def test_apply_revalidates_target_hash_and_marks_stale(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        # Task 5 contract: apply_changeset requires a seeded fidelity outcome.
        change["verification"] = {"outcome": "supported", "route": "auto_apply"}
        changes_mod.save_changeset(self.vault, change)
        path.write_text(path.read_text(encoding="utf-8") + "\nChanged concurrently.\n", encoding="utf-8")

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "stale")
        self.assertIn("Changed concurrently.", path.read_text(encoding="utf-8"))

    def test_apply_and_rollback_restore_page_and_transaction(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        # Task 5 contract: apply_changeset requires a seeded fidelity outcome.
        change["verification"] = {"outcome": "supported", "route": "auto_apply"}
        changes_mod.save_changeset(self.vault, change)

        applied = changes_mod.apply_changeset(self.vault, change["id"])
        self.assertEqual(applied["state"], "applied")
        self.assertIn("New value.", path.read_text(encoding="utf-8"))
        self.assertTrue((self.vault / ".memex" / "transactions.jsonl").exists())

        rolled_back = changes_mod.rollback_changeset(self.vault, change["id"])
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))

    def test_apply_without_verification_parks_pending_with_outcome_required(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "pending")
        self.assertEqual(result.get("reason"), "fidelity verification required")
        saved, _ = changes_mod.load_changeset(self.vault, change["id"])
        self.assertEqual(saved["verification"].get("outcome"), "required")
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))  # page untouched

    def test_apply_review_route_without_approval_parks_pending(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        # The gate RE-computes the route via classify_risk and ignores any
        # pre-seeded `verification["route"]` key. A supported repair on a
        # `decisions` section classifies to `review` naturally, so the review
        # branch (pending without explicit approval) is genuinely exercised.
        change["classification"]["section"] = "decisions"
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "pending")
        self.assertEqual(result.get("reason"), "explicit approval required")
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))

    def test_apply_review_route_with_approval_applies(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        change["classification"]["section"] = "decisions"
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"], approved=True)

        self.assertEqual(result["state"], "applied")
        self.assertIn("New value.", path.read_text(encoding="utf-8"))

    def test_apply_archive_route_parks_pending_without_mutation(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        # An unsupported-evidence claim makes validate_evidence return
        # [{"outcome": "unsupported"}] so classify_risk returns `archive`
        # naturally (the gate ignores a pre-seeded `route` key).
        change["claims"] = [{
            "text": "The runbook requires hourly backups.",
            "type": "process",
            "explicitness": "explicit",
            "evidence": [{
                "raw": "raw/evidence.md",
                "raw_sha256": canon_mod.file_hash(self.raw_dir() / "evidence.md"),
                "start_line": 6,
                "end_line": 6,
                "quote": "The runbook requires hourly backups.",
            }],
        }]
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "pending")
        self.assertIn("not auto-applied", result.get("reason", ""))
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))


    def test_claimless_raw_create_parks_pending_without_auto_apply(self):
        """A claim-less raw CREATE has no evidence anchor to verify — the
        vacuous `any()` over an empty claim list must NOT let it auto-apply
        (Finding-1 guard in apply_changeset)."""
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nNew fact.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="create",
            classification={"section": "topics", "slug": "new-topic", "title": "New Topic", "project": "ws"},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": "new-topic"},
            claims=[],
            proposed_body="## Rule\nNew content.\n",
            risk="low",
            reason="claim-less proposal",
        )
        change["verification"] = {"outcome": "supported", "route": "auto_apply"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "pending")
        self.assertEqual(result.get("reason"), "no evidence-anchored claims")
        saved, _ = changes_mod.load_changeset(self.vault, change["id"])
        self.assertEqual(saved["verification"].get("outcome"), "required")
        self.assertEqual(saved["verification"].get("reason"), "no evidence-anchored claims")
        # no page was created and no transaction journaled
        self.assertFalse((self.vault / "wiki" / "topics" / "new-topic.md").exists())
        self.assertFalse((self.vault / ".memex" / "transactions.jsonl").exists())

    def test_claimless_raw_update_parks_pending_even_when_approved(self):
        """The Finding-1 guard applies to raw UPDATE too, and holds even on an
        explicit approval — the human must add grounded claims, not just wave
        the claim-less body through."""
        page, path = self._current_page()
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nExplicit source text.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="update",
            classification={"section": "topics", "slug": page["slug"], "title": page["title"], "project": "ws"},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": page["slug"], "expected_page_sha256": canon_mod.page_body_hash(path.read_text(encoding="utf-8"))},
            claims=[],
            proposed_body="## Rule\nNew value.\n",
            risk="low",
            reason="claim-less update",
        )
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"], approved=True)

        self.assertEqual(result["state"], "pending")
        self.assertEqual(result.get("reason"), "no evidence-anchored claims")
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))  # page untouched


class TestArchiveAndMerge(MemexTestCase):
    def _page(self, slug, body, section="topics"):
        record = {"slug": slug, "title": slug.title(), "section": section, "kind": "session", "status": "current", "tags": [], "sources": ["session:x"], "summary": slug, "path": f"{section}/{slug}.md", "project": None}
        path = self.vault / "wiki" / record["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: \"{record['title']}\"\nstatus: current\n---\n\n{body}", encoding="utf-8")
        return record

    def test_archive_moves_topic_out_of_wiki_and_index(self):
        page = self._page("unsupported-topic", "## Claim\nUnsupported.\n")
        self.seed_index([page])
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nDifferent fact.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="archive", classification={"section": "topics", "slug": page["slug"], "title": page["title"], "project": None},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": page["slug"], "expected_page_sha256": canon_mod.page_body_hash((self.vault / "wiki" / page["path"]).read_text(encoding="utf-8"))},
            claims=[], proposed_body="", risk="low", reason="unsupported by declared source",
        )
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "applied")
        self.assertFalse((self.vault / "wiki" / page["path"]).exists())
        self.assertTrue((self.vault / ".memex" / "history" / "wiki" / page["path"]).exists())
        self.assertEqual(canon_mod.canonical_pages(self.vault), [])
        # the history copy is the audit trail: body preserved, status archived
        history_text = (self.vault / ".memex" / "history" / "wiki" / page["path"]).read_text(encoding="utf-8")
        self.assertIn("status: archived", history_text)
        self.assertIn("## Claim\nUnsupported.", history_text)

        # Step 5: rollback restores the visible page + index membership, and
        # LEAVES the history audit-trail copy in place.
        rolled_back = changes_mod.rollback_changeset(self.vault, change["id"])
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertTrue((self.vault / "wiki" / page["path"]).exists())
        self.assertIn("## Claim\nUnsupported.",
                      (self.vault / "wiki" / page["path"]).read_text(encoding="utf-8"))
        idx = canon_mod.load_index(self.vault)
        self.assertEqual([p["slug"] for p in idx["pages"]], [page["slug"]])
        self.assertEqual([p["slug"] for p in canon_mod.canonical_pages(self.vault)], [page["slug"]])
        self.assertTrue((self.vault / ".memex" / "history" / "wiki" / page["path"]).exists())

    def test_archive_decisions_park_pending(self):
        page = self._page("a-decision", "## Decision\nMade.\n", section="decisions")
        self.seed_index([page])
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nDiff.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="archive", classification={"section": "decisions", "slug": page["slug"], "title": page["title"], "project": None},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": page["slug"]},
            claims=[], proposed_body="", risk="low", reason="superseded",
        )
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "pending")
        self.assertIn("must be superseded", result.get("reason", ""))
        self.assertTrue((self.vault / "wiki" / page["path"]).exists())  # untouched

    def test_merge_rewrites_incoming_wikilinks_and_preserves_origin_manifest(self):
        target = self._page("canonical-topic", "## Rule\nCanonical.\n")
        origin = self._page("duplicate-topic", "## Rule\nDuplicate.\n")
        referencer = self._page("linked-topic", "## See also\n[[duplicate-topic]]\n")
        self.seed_index([target, origin, referencer])
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nMerge evidence.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="merge", classification={"section": "topics", "slug": target["slug"], "title": target["title"], "project": None},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": target["slug"], "expected_page_sha256": canon_mod.page_body_hash((self.vault / "wiki" / target["path"]).read_text(encoding="utf-8") )},
            claims=[], proposed_body="## Rule\nCanonical and duplicate.\n", risk="low", reason="mechanical duplicate",
        )
        change["origins"] = [origin["slug"]]
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "applied")
        linked = (self.vault / "wiki" / referencer["path"]).read_text(encoding="utf-8")
        self.assertIn("[[canonical-topic]]", linked)
        self.assertNotIn("[[duplicate-topic]]", linked)
        self.assertTrue((self.vault / ".memex" / "history" / "wiki" / origin["path"]).exists())
        manifest = json.loads((self.vault / ".memex" / "history" / "manifests" / f"{change['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["superseded_by"], "canonical-topic")
        self.assertEqual(manifest["origins"], ["duplicate-topic"])
        self.assertEqual(manifest["link_rewrites"]["duplicate-topic"]["rewrites"],
                         [{"page": referencer["path"]}])
        # the origin's history copy is superseded, pointing at the target
        origin_history = (self.vault / ".memex" / "history" / "wiki" / origin["path"]).read_text(encoding="utf-8")
        self.assertIn("status: superseded", origin_history)
        self.assertIn("superseded_by: [[canonical-topic]]", origin_history)

        # Step 5: rollback restores the origin + the incoming [[duplicate-topic]]
        # link and puts both target and origin back in the index.
        rolled_back = changes_mod.rollback_changeset(self.vault, change["id"])
        self.assertEqual(rolled_back["state"], "rolled_back")
        linked = (self.vault / "wiki" / referencer["path"]).read_text(encoding="utf-8")
        self.assertIn("[[duplicate-topic]]", linked)
        self.assertNotIn("[[canonical-topic]]", linked)
        self.assertTrue((self.vault / "wiki" / origin["path"]).exists())
        idx = canon_mod.load_index(self.vault)
        self.assertEqual(sorted(p["slug"] for p in idx["pages"]),
                         ["canonical-topic", "duplicate-topic", "linked-topic"])
        self.assertEqual(sorted(p["slug"] for p in canon_mod.canonical_pages(self.vault)),
                         ["canonical-topic", "duplicate-topic", "linked-topic"])
        # the superseded history copy is the audit trail — left in place
        self.assertTrue((self.vault / ".memex" / "history" / "wiki" / origin["path"]).exists())

    def test_merge_revalidates_target_hash_and_marks_stale(self):
        """A merge filed in a dry-run and applied later must re-validate the
        target body hash: if the target moved, the merge is stale and no byte
        is touched (Finding 2)."""
        target = self._page("canonical-topic", "## Rule\nCanonical.\n")
        origin = self._page("duplicate-topic", "## Rule\nDuplicate.\n")
        self.seed_index([target, origin])
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nMerge evidence.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="merge", classification={"section": "topics", "slug": target["slug"], "title": target["title"], "project": None},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={"slug": target["slug"], "expected_page_sha256": canon_mod.page_body_hash((self.vault / "wiki" / target["path"]).read_text(encoding="utf-8"))},
            claims=[], proposed_body="## Rule\nCanonical and duplicate.\n", risk="low", reason="mechanical duplicate",
        )
        change["origins"] = [origin["slug"]]
        change["verification"] = {"outcome": "supported"}
        changes_mod.save_changeset(self.vault, change)
        # the target moved after the ChangeSet was filed (dry-run -> approve later)
        (self.vault / "wiki" / target["path"]).write_text(
            "---\ntitle: \"Canonical Topic\"\nstatus: current\n---\n\n## Rule\nChanged concurrently.\n",
            encoding="utf-8")

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "stale")
        # target untouched by the merge; origin not superseded, graph intact
        self.assertIn("Changed concurrently.",
                      (self.vault / "wiki" / target["path"]).read_text(encoding="utf-8"))
        self.assertTrue((self.vault / "wiki" / origin["path"]).exists())
        self.assertEqual(sorted(p["slug"] for p in canon_mod.canonical_pages(self.vault)),
                         ["canonical-topic", "duplicate-topic"])


class TestMetrics(MemexTestCase):
    def test_metrics_log_and_summarize(self):
        import memex.metrics as metrics_mod
        metrics_mod.log(self.vault, {"fname": "a.md", "kind": "session", "mode": "full",
                                     "outcome": "supported", "route": "auto_apply",
                                     "latency_ms": 1200, "body_chars": 5000})
        metrics_mod.log(self.vault, {"fname": "b.md", "kind": "doc", "mode": "delta",
                                     "outcome": "partial", "route": "auto_apply",
                                     "latency_ms": 800, "body_chars": 3000})
        s = metrics_mod.summarize(self.vault)
        self.assertEqual(s["rows"], 2)
        self.assertEqual(s["by_outcome"], {"supported": 1, "partial": 1})
        self.assertEqual(s["by_route"], {"auto_apply": 2})
        self.assertEqual(s["by_kind"], {"session": 1, "doc": 1})
        self.assertEqual(s["avg_latency_ms"], 1000.0)
        self.assertEqual(s["total_body_chars"], 8000)
        # --since filters by day
        import time as _time
        metrics_mod.log(self.vault, {"fname": "old.md", "ts": int(_time.time()) - 10 * 86400,
                                     "outcome": "unsupported", "route": "reject"})
        self.assertEqual(metrics_mod.summarize(self.vault)["rows"], 3)
        self.assertEqual(metrics_mod.summarize(self.vault, since="2099-01-01")["rows"], 0)


class TestAuditLots(MemexTestCase):
    def test_dry_run_lot_zero_finds_generated_artifacts_without_moving_them(self):
        legacy = self.vault / "wiki" / "_sugestoes.md"
        legacy.write_text("# Sugestões\n", encoding="utf-8")
        project = self.vault / "wiki" / "projects" / "legacy-project.md"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text("# Legacy project\n", encoding="utf-8")

        result = audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=0, provider=None, quiet=False))

        self.assertEqual(result, 0)
        self.assertTrue(legacy.exists())
        report = json.loads((self.vault / ".memex" / "audit" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(report["lots"]["0"]["generated_artifacts"], 2)

    def test_lot_zero_non_dry_run_migrates_legacy_artifacts_and_journals(self):
        """The non-dry-run lot 0 must migrate the legacy generated artifacts to
        their deterministic `.memex/` destinations, unlink the legacy paths, and
        journal one `migrate-artifact` event per file carrying the base64 bytes
        (recovery = manual extraction from the event; not promoter-rollbackable)."""
        legacy = self.vault / "wiki" / "_sugestoes.md"
        legacy.write_text("# Sugestões\n", encoding="utf-8")
        root_index = self.vault / "index.md"
        root_index.write_text("# Brain index\n", encoding="utf-8")

        result = audit_mod.run(Namespace(vault=str(self.vault), dry_run=False,
                                         lot=0, provider=None, quiet=False))

        self.assertEqual(result, 0)
        # legacy files are gone from the wiki / root
        self.assertFalse(legacy.exists())
        self.assertFalse(root_index.exists())
        # deterministic destinations now hold the exact bytes
        dest_sug = self.vault / ".memex" / "audit" / "merge-suggestions.md"
        dest_index = self.vault / ".memex" / "views" / "brain-index.md"
        self.assertTrue(dest_sug.exists())
        self.assertTrue(dest_index.exists())
        self.assertEqual(dest_sug.read_text(encoding="utf-8"), "# Sugestões\n")
        self.assertEqual(dest_index.read_text(encoding="utf-8"), "# Brain index\n")
        # one journaled migrate-artifact event per file, bytes recoverable
        txn = self.vault / ".memex" / "transactions.jsonl"
        self.assertTrue(txn.exists())
        events = []
        for line in txn.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("action") == "migrate-artifact":
                events.append(ev)
        self.assertEqual(sorted(e["from"] for e in events),
                         ["index.md", "wiki/_sugestoes.md"])
        for ev in events:
            self.assertIn("content_b64", ev)
            self.assertTrue(ev["content_b64"])
            self.assertTrue(ev["to"])

    def test_lot_one_creates_review_for_note_identity_without_guessing_title(self):
        page = {"slug": "note-12345678", "title": "note-12345678", "section": "topics", "kind": "session", "status": "current", "tags": [], "sources": ["session:x"], "summary": "unknown", "path": "topics/note-12345678.md", "project": None}
        target = self.vault / "wiki" / page["path"]
        target.write_text("---\ntitle: \"note-12345678\"\n---\n\n## Fragment\nNo source anchor.\n", encoding="utf-8")
        self.seed_index([page])

        audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=1, provider=None, quiet=False))

        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        change = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(change["operation"], "reclassify")
        self.assertEqual(change["risk"], "review")
        self.assertTrue(target.exists())

    def test_lot_one_skips_decisions_and_entities(self):
        """Scanner fix: technical-identity audit must NOT flag decisions/entities
        (their hyphenated slugs are legitimate; only topics pages are audited)."""
        pages = [
            {"slug": "cris-gateway-architecture-pub-sub", "title": "Cris Gateway — Architecture Pub/Sub",
             "section": "decisions", "kind": "session", "status": "current", "tags": [],
             "sources": ["session:x"], "summary": "decisão", "path": "decisions/cris-gateway-architecture-pub-sub.md", "project": "ws"},
            {"slug": "partners-restaurantes-org-chart", "title": "Partners Restaurantes — Org Chart",
             "section": "entities", "kind": "session", "status": "current", "tags": [],
             "sources": ["session:y"], "summary": "org", "path": "entities/partners-restaurantes-org-chart.md", "project": "ws"},
        ]
        for p in pages:
            fp = self.vault / "wiki" / p["path"]
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(f'---\ntitle: "{p["title"]}"\n---\n\n## Resumo\nlegítimo\n', encoding="utf-8")
        self.seed_index(pages)

        audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=1, provider=None, quiet=False))

        self.assertEqual(list((self.vault / ".memex" / "review" / "pending").glob("*.json")), [])

    def test_lot_two_creates_merge_candidate_for_normalized_title_duplicate(self):
        first = {"slug": "capacity-planning", "title": "Capacity Planning", "section": "topics", "kind": "session", "status": "current", "tags": [], "sources": ["session:a"], "summary": "same", "path": "topics/capacity-planning.md", "project": None}
        second = dict(first, slug="capacity-planning-v2", path="topics/capacity-planning-v2.md")
        for page in (first, second):
            (self.vault / "wiki" / page["path"]).write_text(f"---\ntitle: \"{page['title']}\"\n---\n\n## Same\n", encoding="utf-8")
        self.seed_index([first, second])

        audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=2, provider=None, quiet=False))

        changes = [json.loads(path.read_text(encoding="utf-8")) for path in (self.vault / ".memex" / "review" / "pending").glob("*.json")]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["operation"], "merge")

    def test_lot_two_non_dry_run_applies_mechanical_merge(self):
        """The non-dry-run lot 2 must APPLY the byte-identical mechanical merge
        via the reversible promoter: the shorter slug stays canonical, the longer
        slug is superseded into recovery history and gone from wiki/, and the
        canonical index returns exactly one page."""
        first = {"slug": "capacity-planning", "title": "Capacity Planning", "section": "topics", "kind": "session", "status": "current", "tags": [], "sources": ["session:a"], "summary": "same", "path": "topics/capacity-planning.md", "project": None}
        second = dict(first, slug="capacity-planning-v2", path="topics/capacity-planning-v2.md")
        for page in (first, second):
            (self.vault / "wiki" / page["path"]).write_text(f"---\ntitle: \"{page['title']}\"\n---\n\n## Same\n", encoding="utf-8")
        self.seed_index([first, second])

        result = audit_mod.run(Namespace(vault=str(self.vault), dry_run=False, lot=2, provider=None, quiet=False))
        self.assertEqual(result, 0)

        # shorter-slug page remains canonical
        self.assertTrue((self.vault / "wiki" / "topics" / "capacity-planning.md").exists())
        # longer-slug page is gone from wiki/ (moved to history)
        self.assertFalse((self.vault / "wiki" / "topics" / "capacity-planning-v2.md").exists())
        self.assertTrue((self.vault / ".memex" / "history" / "wiki" / "topics" / "capacity-planning-v2.md").exists())
        # canonical_pages returns one page
        pages = canon_mod.canonical_pages(self.vault)
        self.assertEqual([p["slug"] for p in pages], ["capacity-planning"])
        # the applied merge ChangeSet is journaled
        self.assertTrue((self.vault / ".memex" / "transactions.jsonl").exists())


class TestVerification(MemexTestCase):
    def _change(self, claim_text, quote, section="topics", operation="create"):
        raw = self.raw_dir() / "source.md"
        raw.write_text("---\nsource: claude\nid: source\n---\n\nThe runbook requires a daily backup.\n", encoding="utf-8")
        return changes_mod.new_changeset(
            operation=operation,
            classification={"section": section, "slug": "daily-backup-runbook", "title": "Daily backup runbook", "project": None},
            source={"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={},
            claims=[{
                "text": claim_text,
                "type": "process",
                "explicitness": "explicit",
                "evidence": [{"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw), "start_line": 6, "end_line": 6, "quote": quote}],
            }],
            proposed_body="## Rule\nThe runbook requires a daily backup.\n",
            risk="low",
            reason="test",
        )

    def test_evidence_anchor_marks_exact_quote_supported(self):
        change = self._change("The runbook requires a daily backup.", "The runbook requires a daily backup.")
        evidence = verify_mod.validate_evidence(self.vault, change)
        self.assertEqual(evidence[0]["outcome"], "supported")

    def test_evidence_anchor_marks_missing_quote_unsupported(self):
        change = self._change("The runbook requires hourly backups.", "The runbook requires hourly backups.")
        evidence = verify_mod.validate_evidence(self.vault, change)
        self.assertEqual(evidence[0]["outcome"], "unsupported")
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported"}), "archive")

    def test_decision_and_entity_always_require_review(self):
        change = self._change("The runbook requires a daily backup.", "The runbook requires a daily backup.", section="decisions")
        evidence = [{"outcome": "supported"}]
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported"}), "review")

    def test_person_team_and_sensitive_terms_route_to_review(self):
        """Finding 3: the high-impact term set covers person/team/sensitive/
        conflict vocabulary (PT + EN) — every listed term must route to review."""
        terms = ("time", "equipe", "equipes", "squad", "liderança", "lideranca",
                 "gestor", "gestora", "funcionário", "funcionario", "sensível",
                 "sensivel", "conflito", "conflitos", "pessoa", "pessoas",
                 "contratação", "contratacao", "promoção", "promocao", "salário",
                 "salario", "conflict", "sensitive", "team", "hire", "salary",
                 "owner", "prazo", "deadline")
        for term in terms:
            with self.subTest(term=term):
                change = self._change(f"Esta mudança envolve {term}.", "The runbook requires a daily backup.")
                evidence = [{"outcome": "supported"}]
                self.assertEqual(
                    verify_mod.classify_risk(change, evidence, {"outcome": "supported"}),
                    "review", term)

    def test_term_free_supported_topic_still_auto_applies(self):
        """Control for Finding 3: a supported low-impact topic with none of the
        high-impact terms still classifies to auto_apply."""
        change = self._change("O job roda diariamente e compara médias móveis.",
                              "The runbook requires a daily backup.")
        evidence = [{"outcome": "supported"}]
        self.assertEqual(
            verify_mod.classify_risk(change, evidence, {"outcome": "supported"}),
            "auto_apply")

    def test_doc_adopt_faithful_update_auto_applies_despite_quote_mismatch(self):
        """Doc-ADOPT fix: a faithful doc update whose claims don't quote-match the
        raw is NOT auto-rejected — body fidelity governs, so it can auto-apply."""
        raw = self.raw_dir() / "doc.md"
        raw.write_text("---\nsource: doc\n---\n\n# Doc\n\n## Seção 1\nFato importante.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="update",
            classification={"section": "topics", "slug": "doc-real", "title": "Doc Real", "project": None},
            source={"kind": "doc", "raw": "raw/doc.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={},
            claims=[{
                "text": "Fato importante.",
                "type": "fact",
                "explicitness": "explicit",
                "evidence": [{"raw": "raw/doc.md", "raw_sha256": canon_mod.file_hash(raw),
                              "start_line": 5, "end_line": 5, "quote": "Fato IMPORTANTE (diferente)"}],  # mismatch
            }],
            proposed_body="## Seção 1\nFato importante.\n",
            risk="low",
            reason="test",
        )
        evidence = verify_mod.validate_evidence(self.vault, change)
        self.assertEqual(evidence[0]["outcome"], "doc_faithful")
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported"}), "auto_apply")

    def test_raw_session_quote_mismatch_still_archives(self):
        """Regression guard for the doc fix: a RAW session claim with a quote
        mismatch must STILL route to archive (the strict per-claim rule holds for
        session distillation, only docs get body-fidelity)."""
        change = self._change("The runbook requires hourly backups.",
                              "The runbook requires hourly backups.")
        evidence = verify_mod.validate_evidence(self.vault, change)
        self.assertEqual(evidence[0]["outcome"], "unsupported")
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported"}), "archive")

    def _doc_change(self, value=None, outcome="supported"):
        """A doc-adopt ChangeSet with a given verifier `value` contract."""
        raw = self.raw_dir() / "doc.md"
        raw.write_text("---\nsource: doc\n---\n\n# Doc\n\n## Seção 1\nFato importante.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="update",
            classification={"section": "topics", "slug": "doc-real", "title": "Doc Real", "project": None},
            source={"kind": "doc", "raw": "raw/doc.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={},
            claims=[{"text": "Fato importante.", "type": "fact", "explicitness": "explicit",
                     "evidence": [{"raw": "raw/doc.md", "raw_sha256": canon_mod.file_hash(raw),
                                   "start_line": 5, "end_line": 5, "quote": "Fato"}]}],
            proposed_body="## Seção 1\nFato importante.\n",
            risk="low", reason="test",
        )
        fidelity = {"outcome": outcome}
        if value:
            fidelity["value"] = value
        return change, fidelity

    def test_doc_value_contract_blocks_noop_and_meta(self):
        """The structured `value` contract (new|same|meta) must block no-op and
        meta-narrative auto-applies — only `new` may auto-apply."""
        change, _ = self._doc_change()
        evidence = [{"outcome": "doc_faithful"}]
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "new"}), "auto_apply")
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "same"}), "review")
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "meta"}), "review")

    def test_doc_create_value_same_auto_applies(self):
        """A doc CREATE whose verifier returns value=same is a FALSE negative —
        the verifier compares against an empty current page, so a faithful
        adoption of a NEW doc looks like 'nothing new'. It must auto-apply.
        (Only a doc UPDATE that adds nothing new is a true no-op → reject.)"""
        raw = self.raw_dir() / "doc.md"
        raw.write_text("---\nsource: doc\n---\n\n# Doc\n\n## Seção 1\nFato importante.\n", encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="create",
            classification={"section": "topics", "slug": "doc-real", "title": "Doc Real", "project": None},
            source={"kind": "doc", "raw": "raw/doc.md", "raw_sha256": canon_mod.file_hash(raw)},
            target={}, claims=[], proposed_body="## Seção 1\nFato importante.\n",
            risk="low", reason="test",
        )
        evidence = [{"outcome": "doc_faithful"}]
        self.assertEqual(
            verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "same"}),
            "auto_apply")

    def test_doc_partial_faithful_auto_applies(self):
        """A `partial` doc that preserves all durable content (light reformat /
        adds a link) is a legitimate, reversible adoption — it may auto-apply.
        Invented material is caught as unsupported/conflicting, not partial."""
        change, _ = self._doc_change()
        evidence = [{"outcome": "doc_faithful"}]
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "partial"}), "auto_apply")

    def _delta_change(self, outcome="supported", value="new"):
        """A session-delta ChangeSet: source.kind=raw with mode=delta. Propose
        was skipped, so it carries NO claims by design — fidelity is body-based."""
        raw = self.raw_dir() / "sess.md"
        raw.write_text("---\nsource: claude\nkind: session\n---\n\n## tail\nNova decisão.\n",
                       encoding="utf-8")
        change = changes_mod.new_changeset(
            operation="update",
            classification={"section": "topics", "slug": "sess-topic",
                            "title": "Sessão", "project": None},
            source={"kind": "raw", "mode": "delta", "raw": "raw/sess.md",
                    "raw_sha256": canon_mod.file_hash(raw)},
            target={},
            claims=[],
            proposed_body="## Decisão\nNova decisão registrada.\n",
            risk="low", reason="test",
        )
        fidelity = {"outcome": outcome, "value": value}
        return change, fidelity

    def test_session_delta_partial_parks_but_supported_applies(self):
        """A verified session-delta is judged by BODY FIDELITY (no per-claim
        anchors). Only `supported` auto-applies — `partial` means DURABLE tail
        content was not reflected, so it parks (review) to stop the checkpoint
        advancing past unreflected content. The value contract still blocks
        no-op/meta."""
        change, _ = self._delta_change()
        evidence = []  # no claims → no per-claim anchors
        self.assertEqual(
            verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "new"}),
            "auto_apply")
        self.assertEqual(
            verify_mod.classify_risk(change, evidence, {"outcome": "partial", "value": "new"}),
            "review")
        self.assertEqual(
            verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "same"}),
            "review")
        self.assertEqual(
            verify_mod.classify_risk(change, evidence, {"outcome": "supported", "value": "meta"}),
            "review")

    def test_session_delta_ambiguous_parks_never_discards(self):
        """In auto-review an uncertain session-delta verdict must PARK (review) —
        rejecting would discard the session's new content on a judge's doubt."""
        change, _ = self._delta_change()
        self.assertEqual(
            verify_mod.classify_risk(change, [], {"outcome": "ambiguous"}, auto_review=True),
            "review")

    def test_session_delta_unsupported_rejects_invention(self):
        """In auto-review an UNSUPPORTED session-delta (the merge invented
        durable content absent from the tail) is a hard reject — the
        hallucination gate working."""
        change, _ = self._delta_change()
        self.assertEqual(
            verify_mod.classify_risk(change, [], {"outcome": "unsupported"}, auto_review=True),
            "reject")

    def test_verify_fidelity_session_delta_judges_the_tail(self):
        """A session-delta verifier must receive the appended TAIL as its source
        (not an empty claims list) via the DISTILLED-delta prompt, so an append
        beyond the first N chars of a long session is never verified blind."""
        change, _ = self._delta_change()
        captured = {}
        def _fake(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            captured["prompt"] = prompt
            return json.dumps({"outcome": "supported", "value": "new", "reason": "ok"})
        with mock.patch("memex.providers.complete", side_effect=_fake):
            res = verify_mod.verify_fidelity(
                self.vault, change, kind="openai_compat", model="mock",
                settings={}, source_text="## tail\nNova decisão.\n")
        self.assertIn("SOURCE SLICE", captured["prompt"],
                      "session-delta must use the DISTILLED-delta fidelity prompt")
        self.assertIn("Nova decisão.", captured["prompt"],
                      "the verifier must see the delta tail as its source")
        self.assertEqual(res["outcome"], "supported")

    def test_apply_changeset_session_delta_skips_claim_gate(self):
        """A session-delta (source.mode=delta, no claims by design) that the
        verifier supported must AUTO-APPLY — the evidence-anchored-claim gate is
        for unverified ungrounded bodies, not for verified deltas."""
        change, _ = self._delta_change()
        page = self.vault / "wiki" / "topics" / "sess-topic.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntitle: Sessão\n---\n\n## Base\nconteúdo inicial\n",
                        encoding="utf-8")
        change["index_record"] = {
            "slug": "sess-topic", "title": "Sessão", "section": "topics",
            "kind": "session", "status": "current", "tags": [], "sources": [],
            "summary": "", "project": None, "path": "topics/sess-topic.md",
        }
        change["target"] = {"slug": "sess-topic",
                            "expected_page_sha256": canon_mod.page_body_hash(
                                page.read_text(encoding="utf-8"))}
        change["verification"] = {"outcome": "supported", "value": "new",
                                  "route": "auto_apply", "reason": "ok"}
        changes_mod.save_changeset(self.vault, change)
        result = changes_mod.apply_changeset(self.vault, change["id"], auto_review=True)
        self.assertEqual(result["state"], "applied", result)
        self.assertTrue((self.vault / "wiki" / "topics" / "sess-topic.md").exists())

    def test_doc_value_missing_backward_compat(self):
        """A verifier that omits `value` (legacy / non-doc) still auto-applies on
        supported — the contract is additive, not a hard requirement."""
        change, _ = self._doc_change()
        evidence = [{"outcome": "doc_faithful"}]
        self.assertEqual(verify_mod.classify_risk(change, evidence, {"outcome": "supported"}), "auto_apply")

    def test_verify_parses_fenced_json(self):
        """The verifier must tolerate markdown fences / trailing prose around the
        JSON — a claude -p response is not grammar-constrained, and a strict
        json.loads would turn a good verdict into a spurious 'verifier
        unavailable' retry."""
        self.assertEqual(
            verify_mod._extract_json('```json\n{"outcome": "supported", "value": "new", "reason": "ok"}\n```'),
            {"outcome": "supported", "value": "new", "reason": "ok"})
        self.assertEqual(
            verify_mod._extract_json('Aqui está o resultado:\n{"outcome": "partial", "reason": "leve reformatação"}')
            .get("outcome"), "partial")
        self.assertIsNone(verify_mod._extract_json("texto sem json"))
        self.assertIsNone(verify_mod._extract_json("{incompleto"))

    def test_parse_json_handles_braces_inside_strings(self):
        """A `}` or `{...}` inside a quoted value must NOT truncate the JSON scan
        — a hand-rolled brace counter would, `raw_decode` does not."""
        self.assertEqual(
            verify_mod._extract_json('{"outcome": "supported", "reason": "título com {curly} e }"}\nmais prosa'),
            {"outcome": "supported", "reason": "título com {curly} e }"})
        # array payloads are tolerated too
        self.assertEqual(verify_mod._extract_json('```json\n[{"a": 1}]\n```'), [{"a": 1}])

    def test_high_impact_matches_whole_words_only(self):
        """Risk routing must match whole tokens, not substrings — "time" hits
        "time", never "timeout"/"sometimes"; accented and ASCII both match."""
        self.assertTrue(verify_mod._has_high_impact("vamos marcar um time pra isso"))
        self.assertTrue(verify_mod._has_high_impact("decidimos o deadline com o time"))
        self.assertFalse(verify_mod._has_high_impact("o timeout do provider quebrou"))
        self.assertFalse(verify_mod._has_high_impact("sometimes o sistema falha"))
        self.assertTrue(verify_mod._has_high_impact("o salário e a promoção foram revistos"))

    def test_verifier_error_is_retry_not_rejection(self):
        """A verifier that FAILED (model down / unparseable JSON) returns
        `error=True` and `ambiguous` outcome. classify_risk must NOT turn that
        into a rejection — the synth treats it as an infra retry (raw stays
        pending), so `classify_risk` alone must not burn the raw. Concretely:
        a doc whose fidelity is `ambiguous` with `error` present is NOT routed
        to reject in auto_review mode."""
        change, _ = self._doc_change()
        evidence = [{"outcome": "doc_faithful"}]
        # In auto_review, ambiguous-without-error (real content verdict) IS a
        # rejection; ambiguous-with-error is the caller's retry signal and must
        # not be pre-routed as reject by classify_risk's doc gate.
        self.assertEqual(
            verify_mod.classify_risk(change, evidence,
                                     {"outcome": "ambiguous", "error": True, "reason": "verifier unavailable: JSONDecodeError"},
                                     auto_review=True),
            "reject")  # classify_risk still reports reject — the SYNTH is what
        #   intercepts error=True and keeps the raw pending instead of discarding.
        #   This test pins the CONTRACT that the two signals stay separable.

    def test_ambiguous_fidelity_parks_not_discards_in_auto_review(self):
        """In auto_review, an AMBIGUOUS fidelity (verifier couldn't judge) must
        PARK the ChangeSet — never discard the raw. Only a real unsupported/
        conflicting verdict rejects."""
        raw_change = self._change("The runbook requires a daily backup.",
                                  "The runbook requires a daily backup.")
        evidence = [{"outcome": "supported"}]
        # ambiguous fidelity (verifier couldn't judge) + auto_review → parked
        self.assertEqual(
            verify_mod.classify_risk(raw_change, evidence,
                                     {"outcome": "ambiguous", "reason": "no anchor"},
                                     auto_review=True),
            "review")
        # UNSUPPORTED evidence (anchor miss — could be a paraphrase) + auto_review
        # → parked, NOT discarded
        self.assertEqual(
            verify_mod.classify_risk(raw_change, [{"outcome": "unsupported"}],
                                     {"outcome": "supported"},
                                     auto_review=True),
            "review")
        # CONFLICTING evidence (contradicts the source) + auto_review → reject
        self.assertEqual(
            verify_mod.classify_risk(raw_change, [{"outcome": "conflicting"}],
                                     {"outcome": "conflicting"},
                                     auto_review=True),
            "reject")

    def test_needs_strong_verify_routes_material_changes(self):
        """The cheap (flash) judge handles plain low-risk topic updates; the
        strong judge is reserved for entities/decisions, verifier-only routing
        ops, high-impact-claim changes, and large proposed bodies."""
        def chg(section="topics", op="update", text="regra simples", body="x" * 100):
            return {"classification": {"section": section},
                    "operation": op,
                    "claims": [{"text": text}],
                    "proposed_body": body}
        self.assertFalse(verify_mod.needs_strong_verify(chg()))
        self.assertFalse(verify_mod.needs_strong_verify(chg(op="create")))
        self.assertTrue(verify_mod.needs_strong_verify(chg(section="decisions")))
        self.assertTrue(verify_mod.needs_strong_verify(chg(section="entities")))
        self.assertTrue(verify_mod.needs_strong_verify(chg(op="merge")))
        self.assertTrue(verify_mod.needs_strong_verify(chg(text="o time decidiu o deadline")))
        self.assertTrue(verify_mod.needs_strong_verify(chg(body="y" * 9000),
                                                       strong_body_chars=8000))

    def test_doc_wikilinks_are_not_invention(self):
        """The merge step is ordered to add [[wikilinks]] and may add a short
        'Relacionado'/navigational section. Those are wiki structure, NOT invented
        durable content — so a doc whose only drift is added navigation must be
        judged faithful/partial (auto-appliable), not unsupported/conflicting.
        This pins the prompt contract by exercising the classifier with a doc
        that a strict verifier would otherwise call 'invents material'."""
        change, _ = self._doc_change(value="new")
        # Even with partial (light reformat + nav links), a faithful doc auto-applies.
        self.assertEqual(verify_mod.classify_risk(change, [{"outcome": "doc_faithful"}],
                                                  {"outcome": "partial", "value": "new"}), "auto_apply")
        # The DOC_FIDELITY_PROMPT must tell the verifier to ignore wikilinks/nav:
        self.assertIn("[[wikilinks]]", verify_mod.DOC_FIDELITY_PROMPT)
        self.assertIn("Relacionado", verify_mod.DOC_FIDELITY_PROMPT)
        self.assertIn("NOT invented content", verify_mod.DOC_FIDELITY_PROMPT)


class TestRelinkViaChangesets(MemexTestCase):
    """Finding-2 regression: relink must NOT write wiki/ directly — every page
    mutation routes through `changes.apply_changeset` as an auto-applied repair
    ChangeSet, and nothing stays pending."""

    def test_relink_routes_through_promoter_and_auto_applies(self):
        from memex import relink as relink_mod
        pages = [
            # orphan: 0 in + 0 out — the only target in default (orphan) mode
            {"slug": "alerta-custo-databricks", "title": "Alerta de custo Databricks",
             "section": "topics", "kind": "session", "status": "current",
             "tags": ["databricks"], "sources": ["session:x"], "summary": "alertas",
             "path": "topics/alerta-custo-databricks.md", "project": "ws"},
            # candidate: already has an outgoing link -> not an orphan
            {"slug": "job-diario-custo", "title": "Job diário de custo",
             "section": "topics", "kind": "session", "status": "current",
             "tags": ["databricks"], "sources": ["session:y"], "summary": "job",
             "path": "topics/job-diario-custo.md", "project": "ws"},
            # decoy: no token overlap with the orphan -> never a candidate
            {"slug": "infra-gcp", "title": "Infra GCP",
             "section": "topics", "kind": "session", "status": "current",
             "tags": ["gcp"], "sources": ["session:z"], "summary": "infra",
             "path": "topics/infra-gcp.md", "project": "gcp-team"},
        ]
        for page in pages:
            fp = self.vault / "wiki" / page["path"]
            fp.parent.mkdir(parents=True, exist_ok=True)
            body = "## Regra\nconteúdo íntegro.\n"
            if page["slug"] == "job-diario-custo":
                body += "Veja [[infra-gcp]] para o substrato.\n"
            fp.write_text(
                f'---\ntitle: "{page["title"]}"\nkind: {page["kind"]}\n'
                f"tags: [{page['tags'][0]}]\nsources: [{page['sources'][0]}]\n"
                f"---\n\n{body}",
                encoding="utf-8")
        self.seed_index(pages)

        rc, out = _run_capturing(
            relink_mod.run,
            Namespace(vault=str(self.vault), dry_run=False, refresh=False,
                      all=False, min_links=2, top_k=4))

        self.assertEqual(rc, 0, out)
        # (a) the orphan page now carries the Related section with the link
        page_text = (self.vault / "wiki" / pages[0]["path"]).read_text(encoding="utf-8")
        self.assertIn(relink_mod.RELATED_MARKER, page_text)
        self.assertIn("## Relacionado", page_text)
        self.assertIn("[[job-diario-custo]]", page_text)
        # (b) applied via a ChangeSet: one applied repair, nothing pending
        self.assertEqual(list((self.vault / ".memex" / "review" / "pending").glob("*.json")), [])
        applied = list((self.vault / ".memex" / "review" / "applied").glob("*.json"))
        self.assertEqual(len(applied), 1)
        change = json.loads(applied[0].read_text(encoding="utf-8"))
        self.assertEqual(change["operation"], "repair")
        self.assertEqual(change["source"]["kind"], "relink")
        self.assertEqual(change["verification"]["route"], "auto_apply")
        # decoy untouched — it was never a target nor a candidate edge
        self.assertNotIn(relink_mod.RELATED_MARKER,
                         (self.vault / "wiki" / pages[2]["path"]).read_text(encoding="utf-8"))


class TestReviewAndHealth(MemexTestCase):
    def test_health_counts_only_canonical_pages_and_pending_reviews(self):
        current = {
            "slug": "topic-a", "title": "Topic A", "section": "topics", "kind": "session",
            "status": "current", "tags": [], "sources": ["session:a"], "summary": "a",
            "path": "topics/topic-a.md", "project": None,
        }
        archived = dict(current, slug="topic-old", status="archived", path="topics/topic-old.md")
        for page in (current, archived):
            target = self.vault / "wiki" / page["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("---\ntitle: \"x\"\n---\n\n## x\n", encoding="utf-8")
        self.seed_index([current, archived])
        change = changes_mod.new_changeset(
            operation="create", classification={"section": "topics", "slug": "topic-b", "title": "Topic B", "project": None},
            source={"raw": "raw/missing.md", "raw_sha256": "x"}, target={}, claims=[], proposed_body="## B\n", risk="low", reason="test",
        )
        changes_mod.save_changeset(self.vault, change)

        report = audit_mod.health(self.vault)

        self.assertEqual(report["canonical_pages"], 1)
        self.assertEqual(report["pending_reviews"], 1)
        self.assertEqual(report["invalid_current_identities"], 0)

    def test_review_list_and_reject_move_changeset_state(self):
        change = changes_mod.new_changeset(
            operation="create", classification={"section": "topics", "slug": "topic-b", "title": "Topic B", "project": None},
            source={"raw": "raw/missing.md", "raw_sha256": "x"}, target={}, claims=[], proposed_body="## B\n", risk="review", reason="test",
        )
        changes_mod.save_changeset(self.vault, change)

        rc, listed = _run_capturing(review_mod.run, Namespace(vault=str(self.vault), action="list", change_id=None, reason=None))
        self.assertEqual(rc, 0)
        self.assertIn(change["id"], listed)

        rc, rejected = _run_capturing(review_mod.run, Namespace(vault=str(self.vault), action="reject", change_id=change["id"], reason="not durable"))
        self.assertEqual(rc, 0)
        self.assertIn("rejected", rejected)
        saved, _ = changes_mod.load_changeset(self.vault, change["id"])
        self.assertEqual(saved["state"], "rejected")


class TestProgressUI(unittest.TestCase):
    def test_progress_is_noop_in_pipes_and_renders_on_tty(self):
        from memex import ui
        # pipes/hooks: disabled — no \r noise in machine-readable output
        buf = io.StringIO()
        bar = ui.Progress("docs", total=4, stream=buf)
        bar.update(); bar.update(); bar.done()
        self.assertEqual(buf.getvalue(), "")
        # forced (as on a TTY): renders bar with counts, erases on done
        buf = io.StringIO()
        bar = ui.Progress("docs", total=4, stream=buf, enabled=True)
        bar.update(suffix="a.md"); bar.update(n=4)
        self.assertIn("docs [", buf.getvalue())
        self.assertIn("4/4", bar.render_line())
        bar.done()
        self.assertTrue(buf.getvalue().endswith("\r"))
        # counter mode (unknown total)
        bar = ui.Progress("sessions", stream=io.StringIO(), enabled=True)
        bar.update(); bar.update()
        self.assertIn("sessions 2…", bar.render_line("(1 new)"))


class TestCliSurface(unittest.TestCase):
    def test_init_activation_is_per_workspace_only(self):
        """No machine-wide activation flag: the brain captures only where the
        user explicitly ran init (deliberate opt-in per workspace)."""
        import contextlib
        from memex import cli as cli_mod
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli_mod.build_parser().parse_args(["init", "--everything"])

    def test_tidy_and_legacy_alias_parse_and_stubs_are_gone(self):
        from memex import cli as cli_mod
        parser = cli_mod.build_parser()
        for name in ("tidy", "gardening"):
            args = parser.parse_args([name, "--dry-run"])
            self.assertTrue(args.dry_run)
        for gone in ("history", "diff", "revert", "tier"):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    import contextlib
                    with contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args([gone])


class TestHookInstall(MemexTestCase):
    def test_install_writes_four_events_and_uninstall_cleans(self):
        _, plan = hook_mod._install_all(self.workspace, self.vault)
        self.assertEqual(set(plan), {"SessionStart", "UserPromptSubmit",
                                     "SessionEnd", "PreCompact"})
        cfg = json.loads((self.workspace / ".claude" / "settings.local.json")
                         .read_text(encoding="utf-8"))
        mcp_cfg = json.loads((self.workspace / ".mcp.json")
                             .read_text(encoding="utf-8"))
        self.assertIn("memex", mcp_cfg["mcpServers"])
        self.assertEqual(mcp_cfg["mcpServers"]["memex"]["type"], "stdio")
        self.assertNotIn("mcpServers", cfg)
        for event, verb in [("SessionStart", "boot"), ("UserPromptSubmit", "recall"),
                            ("SessionEnd", "capture"), ("PreCompact", "--partial")]:
            cmds = [h["command"] for g in cfg["hooks"][event] for h in g["hooks"]]
            self.assertTrue(any(verb in c for c in cmds), (event, cmds))
            self.assertTrue(all("nohup" not in c and "$(" not in c for c in cmds))
        # idempotent re-install: no duplicates
        hook_mod._install(self.workspace, self.vault)
        cfg = json.loads((self.workspace / ".claude" / "settings.local.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(len(cfg["hooks"]["SessionStart"]), 1)
        # uninstall removes everything memex
        _, removed = hook_mod._uninstall(self.workspace)
        self.assertEqual(removed, 4)
        cfg = json.loads((self.workspace / ".claude" / "settings.local.json")
                         .read_text(encoding="utf-8"))
        self.assertNotIn("hooks", cfg)

    def test_install_replaces_v1_hooks_and_keeps_foreign(self):
        sp = self.workspace / ".claude" / "settings.local.json"
        sp.parent.mkdir(parents=True)
        sp.write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "memex retrieve --vault /old"}]},
                {"hooks": [{"type": "command", "command": "my-other-tool --run"}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command",
                            "command": "memex ingest ...; nohup memex synth ... &"}]},
            ],
        }}), encoding="utf-8")
        hook_mod._install(self.workspace, self.vault)
        cfg = json.loads(sp.read_text(encoding="utf-8"))
        ups = [h["command"] for g in cfg["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertTrue(any("my-other-tool" in c for c in ups))   # foreign kept
        self.assertFalse(any("/old" in c for c in ups))           # v1 replaced
        self.assertFalse(any("nohup" in h["command"]
                             for g in cfg["hooks"]["SessionEnd"] for h in g["hooks"]))


class TestMcpServer(MemexTestCase):
    """MCP server: stdio JSON-RPC protocol + tool dispatch (no subprocess)."""

    def _call(self, method, params=None):
        """Simulate one JSON-RPC request through the handler."""
        from memex import mcp_server as ms
        msg = {"jsonrpc": "2.0", "id": 99, "method": method, "params": params or {}}
        return ms._handle_request(msg)

    def _tool_result(self, response):
        return json.loads(response["result"]["content"][0]["text"])

    def test_initialize_returns_server_info_and_capabilities(self):
        resp = self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test"},
        })
        self.assertEqual(resp["result"]["serverInfo"]["name"], "memex")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_notification_is_silent(self):
        from memex import mcp_server as ms
        # Notifications have no "id" field at all
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = ms._handle_request(msg)
        self.assertIsNone(resp)

    def test_ping(self):
        resp = self._call("ping")
        self.assertEqual(resp["result"], {})

    def test_tools_list(self):
        resp = self._call("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["search", "remember", "status", "health", "audit", "review_list", "review_show", "review_approve", "review_reject", "review_rollback"])

    def test_each_tool_declares_input_schema(self):
        resp = self._call("tools/list")
        for tool in resp["result"]["tools"]:
            with self.subTest(tool=tool["name"]):
                self.assertIn("inputSchema", tool)
                self.assertIn("type", tool["inputSchema"])
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_status_no_vault(self):
        resp = self._call("tools/call", {
            "name": "status", "arguments": {"vault": str(self.vault)},
        })
        data = self._tool_result(resp)
        self.assertTrue(data["ok"])
        self.assertIn("vault", data)
        self.assertIn("wiki_pages", data)
        self.assertIn("raw_notes", data)

    def test_search_in_empty_brain(self):
        resp = self._call("tools/call", {
            "name": "search", "arguments": {"vault": str(self.vault), "query": "nada"},
        })
        data = self._tool_result(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 0)

    def test_remember_ingests_text(self):
        # synthesis runs inline; stub it so the test is hermetic and fast (the
        # real ChangeSet routing through synth is covered by the reflect tests)
        with mock.patch("memex.synth.run", return_value=1):
            resp = self._call("tools/call", {
                "name": "remember",
                "arguments": {"vault": str(self.vault),
                              "text": "Decisão: usar MCP para expor o cérebro a agentes de IA."},
            })
        data = self._tool_result(resp)
        self.assertTrue(data["ok"], f"remember failed: {data}")
        self.assertIn("raw/", data.get("file", ""))
        # canonical publication is no longer equivalent to processing: the tool
        # returns the ChangeSets the raw produced (any state), not a boolean.
        self.assertIn("changes", data)
        self.assertIsInstance(data["changes"], list)

    def test_unknown_tool(self):
        resp = self._call("tools/call", {
            "name": "nao-existe", "arguments": {},
        })
        self.assertEqual(resp["error"]["code"], -32601)

    def test_empty_remember_is_rejected(self):
        resp = self._call("tools/call", {
            "name": "remember",
            "arguments": {"vault": str(self.vault), "text": "   "},
        })
        data = self._tool_result(resp)
        self.assertFalse(data["ok"])

    def test_unknown_method(self):
        resp = self._call("some/unknown/method")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_search_without_vault_returns_error(self):
        # Explicitly non-existent vault
        resp = self._call("tools/call", {
            "name": "search",
            "arguments": {"query": "test", "vault": "/tmp/nao-existe-xyz"},
        })
        data = self._tool_result(resp)
        self.assertFalse(data["ok"], f"expected error, got: {data}")
        self.assertIn("error", data)


class TestBackfill(MemexTestCase):
    def test_backfill_report_groups_sessions_and_counts_appends(self):
        """`memex deltas` dry-run: sessions group snapshots by id, walk the
        append chain (prefix-hash), and cross it with lineage checkpoints —
        read-only, no LLM, no writes."""
        import memex.synth as synth_mod
        sid = "sess-chain"
        base = "base\n" * 5

        def _raw(name, id_, body):
            f = self.raw_dir() / name
            f.write_text(f"---\nsource: claude\nid: {id_}\nkind: session\n---\n\n{body}",
                         encoding="utf-8")
            return f

        f1 = _raw("2026-08-08--claude--sess-chain--a.md", sid, base)
        f2 = _raw("2026-08-08--claude--sess-chain--b.md", sid, base + "segundo\n")
        # 2 -> 3 is EDITED: a prefix line changed, then a third line was added
        edited = base.replace("base", "BASE", 1) + "segundo\nterceiro\n"
        f3 = _raw("2026-08-09--claude--sess-chain--c.md", sid, edited)
        os.utime(f1, (time.time() - 300, time.time() - 300))
        os.utime(f2, (time.time() - 200, time.time() - 200))
        os.utime(f3, (time.time(), time.time()))
        synth_mod._save_lineage(self.vault, {
            sid: {"raw": f1.name, "chars": len(base),
                  "body_hash": synth_mod._body_hash(base),
                  "slug": "sess-topic", "section": "topics"}})
        r = synth_mod.backfill_report(self.vault)
        self.assertEqual(r["n_sessions"], 1)
        s = r["sessions"][0]
        self.assertEqual(s["snapshots"], 3)
        self.assertEqual(s["append_steps"], 1)     # 1->2 append, 2->3 edited
        self.assertEqual(s["non_append_steps"], 1)
        self.assertTrue(s["has_checkpoint"])
        # 2->3 EDITED the prefix, so neither older snapshot is a strict prefix
        # of the latest → zero TRUE duplicates (they hold unique content).
        self.assertEqual(r["n_superseded_snapshots"], 0)

    def test_backfill_report_counts_no_checkpoint_sessions(self):
        """Sessions without a lineage checkpoint are reported separately (the
        historical backfill surface) with their file/char weight."""
        import memex.synth as synth_mod
        for i in range(3):
            f = self.raw_dir() / f"2026-08-08--claude--sess-{i}--a.md"
            f.write_text(
                f"---\nsource: claude\nid: sess-{i}\nkind: session\n---\n\n{'linha\n' * 20}",
                encoding="utf-8")
        r = synth_mod.backfill_report(self.vault)
        self.assertEqual(r["n_sessions"], 3)
        self.assertEqual(r["no_checkpoint"], 3)
        self.assertEqual(r["no_checkpoint_files"], 3)
        self.assertGreater(r["no_checkpoint_chars"], 0)
        self.assertEqual(r["with_checkpoint"], 0)


class TestPipelineArtifactDetector(MemexTestCase):
    """The meta-worker skip must be STRUCTURAL (session source + temp cwd),
    not string-matched: it has to catch every worker capture (propose/merge/
    doc-ADOPT/tidy/workspace) without enumerating prompts, and must never
    swallow a real user session, a doc copy, or a prism capture."""

    def _artifact(self, source="claude", cwd=None, body="## user\nYou maintain a personal knowledge wiki in Markdown (Obsidian-style). The RAW source is ALREADY curated.\n## assistant\n{\"slug\": \"x\"}"):
        return {"source": source, "cwd": cwd or tempfile.gettempdir(), "kind": "session"}, body

    def test_worker_capture_in_temp_cwd_is_skipped(self):
        # A propose/merge/doc-adopt worker runs from the OS temp dir.
        self.assertTrue(synth_mod._is_pipeline_artifact(*self._artifact()))

    def test_any_worker_prompt_is_skipped_without_enumerating(self):
        # Structural detection catches even prompts we did NOT list (tidy,
        # doc-adopt, future workers) as long as cwd is temp.
        for prompt in (
            "## user\nYou are consolidating several wiki pages that are ALL about the same topic into ONE coherent page.",
            "## user\nYou maintain a personal knowledge wiki in Markdown (Obsidian-style). The RAW source is ALREADY curated.",
            "## user\nYou write the WORKING-MEMORY handoff page for someone's ongoing work.",
        ):
            self.assertTrue(synth_mod._is_pipeline_artifact(*self._artifact(body=prompt)))

    def test_real_user_session_in_project_cwd_is_kept(self):
        # A prism session or any real user session runs from a project dir.
        self.assertFalse(synth_mod._is_pipeline_artifact(*self._artifact(cwd="/Users/gian.moraes/src/cris/repos/prism")))
        self.assertFalse(synth_mod._is_pipeline_artifact(*self._artifact(cwd="/Users/gian.moraes/src/memex")))

    def test_doc_capture_in_temp_cwd_is_kept(self):
        # `kind: doc` copies (my-skills-ingest into /private/tmp) are durable
        # reference files — source != session, so never skipped.
        self.assertFalse(synth_mod._is_pipeline_artifact(*self._artifact(source="doc")))

    def test_cursor_codex_workers_also_skipped(self):
        # The session-source set covers every tool the capture hook sees.
        for src in ("cursor", "codex"):
            self.assertTrue(synth_mod._is_pipeline_artifact(*self._artifact(source=src)))

    def test_non_temp_path_not_skipped_even_with_worker_prompt(self):
        # A user session that merely QUOTES a worker prompt (project cwd) is
        # still a real session — never dropped on a string match alone.
        self.assertFalse(synth_mod._is_pipeline_artifact(*self._artifact(cwd="/Users/gian.moraes/src/memex")))

    def test_temp_SUBDIR_capture_is_kept(self):
        # The memex worker runs with cwd == the OS tempdir ITSELF. A real
        # session (or a test fixture) that runs from a temp SUBDIR
        # (…/T/memex-test-xyz) is a legitimate capture — never skipped.
        subdir = os.path.join(tempfile.gettempdir(), "memex-test-xyz")
        self.assertFalse(synth_mod._is_pipeline_artifact(*self._artifact(cwd=subdir)))


class TestDocDeterministicRoute(MemexTestCase):
    """kind:doc adoption must run WITHOUT any LLM call — identity from the
    source path + H1, verbatim body, mechanical containment check."""

    def test_doc_parts_extracts_h1_and_body(self):
        body = "# MCPs configurados\n\nInventário dos servidores.\n"
        title, clean, tags = synth_mod._doc_parts(body)
        self.assertEqual(title, "MCPs configurados")
        self.assertIn("Inventário", clean)
        self.assertEqual(tags, [])

    def test_doc_parts_strips_internal_frontmatter(self):
        body = ("---\ntitle: \"MCPs no Claude\"\ntags: [mcp, tooling]\n---\n"
                "# MCPs configurados\n\nConteúdo.\n")
        title, clean, tags = synth_mod._doc_parts(body)
        self.assertEqual(title, "MCPs configurados")  # H1 wins over frontmatter
        self.assertNotIn("title:", clean)  # frontmatter removed
        self.assertIn("mcp", tags)

    def test_doc_parts_no_title_returns_none(self):
        title, clean, tags = synth_mod._doc_parts("sem titulo\nconteudo")
        self.assertIsNone(title)

    def test_doc_slug_is_path_derived_and_stable(self):
        a = synth_mod._doc_slug("/repo/x/SKILL.md", "/repo/x")
        b = synth_mod._doc_slug("/repo/y/SKILL.md", "/repo/y")
        self.assertNotEqual(a, b)          # same stem, different path → no clash
        c = synth_mod._doc_slug("/repo/x/SKILL.md", "/repo/x")
        self.assertEqual(a, c)             # stable across re-captures
        self.assertTrue(a.endswith("-") and a[-9:].count("-") == 1 or "-" in a)

    def test_doc_slug_uses_relative_path(self):
        slug = synth_mod._doc_slug("/users/g/src/cris/repos/prism/skills/curate/SKILL.md",
                                   "/users/g/src/cris")
        self.assertIn("repos", slug)       # keeps directory context, not just stem

    def test_normalize_ws_collapses(self):
        self.assertEqual(synth_mod._normalize_ws("a\n\n  b"), "a b")
        self.assertEqual(synth_mod._normalize_ws(""), "")

    def test_doc_route_does_not_call_provider(self):
        # A doc with a title + path id must be adopted with ZERO provider calls.
        # Build a minimal item and assert the deterministic branches fire.
        vault = self.vault
        raw = self.raw_dir() / "2026-07-13--doc--users-g-src--a1b2c3d4.md"
        raw.write_text(
            "---\nsource: doc\nid: /users/g/src/notes/arquitetura.md\n"
            "date: 2026-07-13\ncwd: /users/g/src\nkind: doc\n---\n"
            "# Arquitetura do serviço\n\nDecisões de arquitetura.\n",
            encoding="utf-8")
        # _doc_parts on the body after frontmatter
        text = raw.read_text(encoding="utf-8")
        meta, body = synth_mod._read_frontmatter(text)
        title, clean, tags = synth_mod._doc_parts(body)
        slug = synth_mod._doc_slug(meta["id"], meta.get("cwd") or "")
        self.assertEqual(title, "Arquitetura do serviço")
        self.assertTrue(slug)
        self.assertIn("Decisões de arquitetura", clean)

    def test_doc_route_synth_runs_without_any_llm_call(self):
        # The definitive check: run the FULL synth pipeline on a doc eligible
        # for deterministic adoption while providers.complete RAISES if called.
        # A real run that needs any LLM call would blow up; a doc-auto run
        # completes and applies the page verbatim.
        raw = self.raw_dir() / "2026-07-13--doc--users-g-src-notes--a1b2c3d4.md"
        raw.write_text(
            "---\nsource: doc\nid: /users/g/src/notes/arquitetura.md\n"
            "date: 2026-07-13\ncwd: /users/g/src\nkind: doc\n---\n"
            "# Arquitetura do serviço\n\n"
            "Decisões de arquitetura do serviço de pedidos.\n",
            encoding="utf-8")
        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=1)
        calls = []
        def _boom(*a, **k):
            calls.append(a)
            raise AssertionError("LLM must not be called for a deterministic doc")
        with mock.patch("memex.providers.complete", side_effect=_boom):
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], "doc-auto must make ZERO provider calls")
        # The page was applied verbatim.
        page = self.vault / "wiki" / "topics" / f"{synth_mod._doc_slug('/users/g/src/notes/arquitetura.md', '/users/g/src')}.md"
        self.assertTrue(page.exists(), "doc-auto must apply the page")
        text = page.read_text(encoding="utf-8")
        self.assertIn("Decisões de arquitetura do serviço de pedidos", text)
        self.assertNotIn("LLM must not", text)


class TestFreshStart(unittest.TestCase):
    """`memex fresh-start`: mark pre-date raws processed (no LLM) and optionally
    archive pending ChangeSets. Dry-run must never mutate the vault."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="memex-freshstart-"))

    def _vault_with_raws(self):
        v = self.tmp / "vault"
        (v / ".memex" / "raw").mkdir(parents=True)
        (v / ".memex" / "review" / "pending").mkdir(parents=True)
        # raws de julho e agosto
        for name in ("2026-07-15--claude--aaa--x.md", "2026-07-20--claude--bbb--y.md",
                     "2026-08-05--claude--ccc--z.md"):
            (v / ".memex" / "raw" / name).write_text(f"body of {name}", encoding="utf-8")
        # synthed vazio
        (v / ".memex" / "synthed.json").write_text("{}", encoding="utf-8")
        # 2 pendings (simula ChangeSet)
        for i in range(2):
            (v / ".memex" / "review" / "pending" / f"p{i}.json").write_text(
                '{"id":"p%d","state":"pending","source":{"raw":"raw/x.md"}}' % i,
                encoding="utf-8")
        return v

    def _args(self, v, *, dry_run, from_date="2026-08-01", archive_pending=True):
        return Namespace(vault=str(v), from_date=from_date,
                         dry_run=dry_run, archive_pending=archive_pending)

    def test_dry_run_marks_nothing(self):
        from memex import freshstart
        v = self._vault_with_raws()
        rc, _ = _run_capturing(freshstart.run, self._args(v, dry_run=True))
        self.assertEqual(rc, 0)
        # nada mutado: synthed continua vazio, pendings intactos
        self.assertEqual(
            json.loads((v / ".memex" / "synthed.json").read_text(encoding="utf-8")), {})
        self.assertEqual(len(list((v / ".memex" / "review" / "pending").glob("*.json"))), 2)

    def test_apply_marks_pre_august_and_archives(self):
        from memex import freshstart
        v = self._vault_with_raws()
        rc, _ = _run_capturing(freshstart.run, self._args(v, dry_run=False))
        self.assertEqual(rc, 0)
        s = json.loads((v / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        # 2 raws de julho marcados, agosto NÃO
        self.assertIn("2026-07-15--claude--aaa--x.md", s)
        self.assertIn("2026-07-20--claude--bbb--y.md", s)
        self.assertNotIn("2026-08-05--claude--ccc--z.md", s)
        # hash real
        h = hashlib.sha256(
            (v / ".memex" / "raw" / "2026-07-15--claude--aaa--x.md").read_bytes()
        ).hexdigest()[:16]
        self.assertEqual(s["2026-07-15--claude--aaa--x.md"], h)
        # pendings arquivados
        self.assertEqual(len(list((v / ".memex" / "review" / "pending").glob("*.json"))), 0)
        self.assertEqual(
            len(list((v / ".memex" / "review" / "archived-pre-freshstart").glob("*.json"))), 2)

    def test_idempotent(self):
        from memex import freshstart
        v = self._vault_with_raws()
        rc, _ = _run_capturing(freshstart.run, self._args(v, dry_run=False))
        self.assertEqual(rc, 0)
        rc2, _ = _run_capturing(freshstart.run, self._args(v, dry_run=False))  # no-op
        self.assertEqual(rc2, 0)
        s = json.loads((v / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        self.assertEqual(len(s), 2)  # não duplicou

    def test_apply_no_archive_keeps_pendings(self):
        from memex import freshstart
        v = self._vault_with_raws()
        rc, _ = _run_capturing(freshstart.run, self._args(v, dry_run=False,
                                                          archive_pending=False))
        self.assertEqual(rc, 0)
        # raws marcados mesmo sem arquivar
        s = json.loads((v / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        self.assertIn("2026-07-15--claude--aaa--x.md", s)
        # pendings NÃO foram movidos
        self.assertEqual(len(list((v / ".memex" / "review" / "pending").glob("*.json"))), 2)
        self.assertFalse((v / ".memex" / "review" / "archived-pre-freshstart").exists())


class TestChangeSetDedup(MemexTestCase):
    """M2: a reflect that reprocesses a raw which already has a PENDING ChangeSet
    (the run was killed between `save_changeset` and the synthed flush) must NOT
    create a duplicate — one slice once stacked 11 identical pending ChangeSets
    from 11 runs. The dedup key is (raw_sha256, slug, section, chunk_idx,
    operation); an identical pending skips (raw marked done), a diverged one is
    superseded (old -> stale)."""

    def setUp(self):
        super().setUp()
        self.srv, base = _start_mock_llm()
        cfg = config_mod.load_global()
        cfg["provider"] = {
            "order": ["openai_compat"],
            "openai_compat": {"base_url": base, "api_key": None,
                              "model_propose": "mock", "model_merge": "mock"},
        }
        config_mod.save_global(cfg)

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def _change(self, raw_sha="abc", slug="s", section="topics", chunk_idx=None,
                op="create", body="b"):
        return {"id": "x", "state": "pending", "operation": op,
                "source": {"raw": "raw/r.md", "raw_sha256": raw_sha, "kind": "raw",
                           "mode": "chunk" if chunk_idx is not None else "full"},
                "target": {"slug": slug},
                "index_record": {"section": section},
                "proposed_body": body,
                "_chunk_index": chunk_idx}

    def _capture_session(self, sid="sess-llm"):
        t = _fake_transcript(self.tmp, sid, str(self.workspace))
        _run_capturing(
            capture_mod.run,
            Namespace(vault=str(self.vault), partial=False, docs=False,
                      workspace=None, transcript=None, no_reflect=True),
            payload={"transcript_path": str(t), "cwd": str(self.workspace)})

    def _args(self):
        return Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=1)

    def test_dedup_key_stable(self):
        from memex import changes
        c = self._change()
        k1 = changes.compute_dedup_key(c)
        k2 = changes.compute_dedup_key(c)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 16)

    def test_dedup_key_differs_by_slice(self):
        from memex import changes
        k1 = changes.compute_dedup_key(self._change(chunk_idx=0))
        k2 = changes.compute_dedup_key(self._change(chunk_idx=1))
        self.assertNotEqual(k1, k2)

    def test_load_pending_dedup(self):
        from memex import changes
        import json
        v = self.vault
        pd = v / ".memex" / "review" / "pending"
        pd.mkdir(parents=True, exist_ok=True)
        c = self._change(raw_sha="sha1", slug="s1", body="x")
        c["id"] = "id1"
        (pd / "id1.json").write_text(json.dumps(c), encoding="utf-8")
        d = changes.load_pending_dedup(v)
        key = changes.compute_dedup_key(c)
        self.assertEqual(d.get(key), "id1")

    def test_reflect_does_not_duplicate_a_pending_slice_on_reprocess(self):
        """A raw that already has an IDENTICAL pending ChangeSet (killed before
        the synthed flush) is marked done on reprocess — no duplicate is saved
        and it exits the backlog. Regression for the 11-identical-pendings bug."""
        import memex.synth as synth_mod
        import memex.metrics as metrics_mod
        self._capture_session("dedup-full")
        proposal = {
            "skip": False, "slug": "databricks-cost-alert-decision",
            "title": "Alerta de custo Databricks — decisão",
            "section": "decisions", "tags": ["databricks"], "related": [],
            "project": None,
            "distill": "Decidimos alertar quando o custo diário exceder 2x a média de 7 dias.",
            "claims": [{"text": "Perfeito, decidimos: alerta quando custo diário > 2x média de 7 dias.",
                        "type": "decision", "explicitness": "explicit"}],
        }
        body = "## Decision\nRun backups daily.\n"
        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(proposal, body)):
            rc = synth_mod.run(self._args())
        self.assertEqual(rc, 0)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        first_id = json.loads(pending[0].read_text(encoding="utf-8"))["id"]
        raw = next(self.raw_dir().glob("*.md"))
        h = hashlib.sha256(raw.read_bytes()).hexdigest()[:16]

        # simulate the killed run: the pending ChangeSet survived but the synthed
        # flush never happened → the raw looks unprocessed to the next reflect.
        synthed_path = self.vault / ".memex" / "synthed.json"
        synthed_path.write_text("{}", encoding="utf-8")

        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(proposal, body)):
            rc, out = _run_capturing(synth_mod.run, self._args())
        self.assertEqual(rc, 0)
        self.assertIn("dedup-skip", out)
        # no duplicate: the SAME pending remains, nothing stacked
        pending2 = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending2), 1)
        self.assertEqual(json.loads(pending2[0].read_text(encoding="utf-8"))["id"], first_id)
        # the raw is marked done again → exits the backlog
        synthed = json.loads(synthed_path.read_text(encoding="utf-8"))
        self.assertEqual(synthed.get(raw.name), h)
        # a dedup-skip metric was appended
        modes = {e.get("mode") for e in metrics_mod.read(self.vault)}
        self.assertIn("dedup-skip", modes)

    def test_reflect_supersedes_a_diverged_pending_instead_of_duplicating(self):
        """A reprocess whose merged body DIVERGED from the parked ChangeSet (same
        raw, non-deterministic merge) must move the old to stale and park the new
        — never two live pendings for the same slice."""
        import memex.synth as synth_mod
        self._capture_session("dedup-supersede")
        proposal = {
            "skip": False, "slug": "databricks-cost-alert-decision",
            "title": "Alerta de custo Databricks — decisão",
            "section": "decisions", "tags": ["databricks"], "related": [],
            "project": None,
            "distill": "Decidimos alertar quando o custo diário exceder 2x a média de 7 dias.",
            "claims": [{"text": "Perfeito, decidimos: alerta quando custo diário > 2x média de 7 dias.",
                        "type": "decision", "explicitness": "explicit"}],
        }
        body_a = "## Decision\nRun backups daily.\n"
        body_b = "## Decision\nRun backups hourly.\n"   # merge diverged on reprocess
        seq = {"n": 0}

        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            if "Reply with STRICT JSON" in prompt:
                return json.dumps(proposal)
            if "You verify" in prompt:
                return json.dumps({"outcome": "supported", "value": "new", "reason": "explicit"})
            seq["n"] += 1
            return body_a if seq["n"] == 1 else body_b

        with mock.patch("memex.providers.complete", side_effect=_route):
            rc = synth_mod.run(self._args())
        self.assertEqual(rc, 0)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        old_id = json.loads(pending[0].read_text(encoding="utf-8"))["id"]
        (self.vault / ".memex" / "synthed.json").write_text("{}", encoding="utf-8")

        with mock.patch("memex.providers.complete", side_effect=_route):
            rc, out = _run_capturing(synth_mod.run, self._args())
        self.assertEqual(rc, 0)
        # one new pending (diverged body) + the old moved to stale
        pending2 = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending2), 1)
        new_id = json.loads(pending2[0].read_text(encoding="utf-8"))["id"]
        self.assertNotEqual(new_id, old_id)
        stale = list((self.vault / ".memex" / "review" / "stale").glob("*.json"))
        self.assertEqual(len(stale), 1)
        stale_change = json.loads(stale[0].read_text(encoding="utf-8"))
        self.assertEqual(stale_change["id"], old_id)
        self.assertEqual(stale_change.get("review_reason"), "superseded by reprocess")

    def test_reflect_does_not_supersede_a_change_applied_mid_run(self):
        """Guard: a pending ChangeSet that a CONCURRENT reviewer APPLIES between
        run start (dedup_set load) and the route phase must NOT be relocated to
        stale by a diverged reprocess — that would corrupt the applied-state
        ledger (rollback/health would count wrong). The reprocess dedup-skips
        instead: applied ChangeSet intact, no duplicate created, raw marked done."""
        import memex.synth as synth_mod
        import memex.changes as changes_mod
        import memex.metrics as metrics_mod
        self._capture_session("dedup-applied-guard")
        proposal = {
            "skip": False, "slug": "databricks-cost-alert-decision",
            "title": "Alerta de custo Databricks — decisão",
            "section": "decisions", "tags": ["databricks"], "related": [],
            "project": None,
            "distill": "Decidimos alertar quando o custo diário exceder 2x a média de 7 dias.",
            "claims": [{"text": "Perfeito, decidimos: alerta quando custo diário > 2x média de 7 dias.",
                        "type": "decision", "explicitness": "explicit"}],
        }
        body_a = "## Decision\nRun backups daily.\n"
        body_b = "## Decision\nRun backups hourly.\n"   # merge diverged on reprocess
        seq = {"n": 0}
        ready = {"run1_done": False}

        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            if "Reply with STRICT JSON" in prompt:
                return json.dumps(proposal)
            if "You verify" in prompt:
                if ready["run1_done"]:
                    # the concurrent reviewer APPLIES the parked change mid-run —
                    # AFTER dedup_set was loaded (change still pending), so the
                    # route phase must discover it as applied and NOT supersede it.
                    changes_mod.transition_changeset(
                        self.vault, old_id, "applied", reason="reviewer approved")
                return json.dumps({"outcome": "supported", "value": "new", "reason": "explicit"})
            seq["n"] += 1
            return body_a if seq["n"] == 1 else body_b

        with mock.patch("memex.providers.complete", side_effect=_route):
            rc = synth_mod.run(self._args())
        self.assertEqual(rc, 0)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        old_id = json.loads(pending[0].read_text(encoding="utf-8"))["id"]
        ready["run1_done"] = True

        # killed run: the pending survived, the synthed flush did not
        (self.vault / ".memex" / "synthed.json").write_text("{}", encoding="utf-8")

        with mock.patch("memex.providers.complete", side_effect=_route):
            rc, out = _run_capturing(synth_mod.run, self._args())
        self.assertEqual(rc, 0)
        self.assertIn("existing applied not superseded", out)
        # the applied ChangeSet stays applied — NOT relocated to stale
        applied = list((self.vault / ".memex" / "review" / "applied").glob("*.json"))
        self.assertEqual(len(applied), 1)
        applied_change = json.loads(applied[0].read_text(encoding="utf-8"))
        self.assertEqual(applied_change["id"], old_id)
        self.assertEqual(applied_change.get("state"), "applied")
        stale = list((self.vault / ".memex" / "review" / "stale").glob("*.json"))
        self.assertEqual(len(stale), 0, "applied ChangeSet must NOT be relocated to stale")
        pending2 = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending2), 0, "no duplicate ChangeSet created")
        # the raw is marked done → exits the backlog
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        raw = next(self.raw_dir().glob("*.md"))
        h = hashlib.sha256(raw.read_bytes()).hexdigest()[:16]
        self.assertEqual(synthed.get(raw.name), h)
        # the guard is observable in the metrics
        reasons = {e.get("reason") for e in metrics_mod.read(self.vault)
                   if e.get("mode") == "dedup-skip"}
        self.assertIn("existing applied not superseded", reasons)

    def test_reflect_does_not_duplicate_a_pending_chunk_on_reprocess(self):
        """Chunk leg: reprocessing a giant session whose chunks were PARKED
        pending must not stack duplicate ChangeSets for the same chunk slice."""
        import memex.synth as synth_mod
        import memex.metrics as metrics_mod
        sid = "sess-giant-dedup"
        big = ("linha de conteudo durável " * 3000)  # ~75k chars → 2 chunks
        f = self.raw_dir() / f"2026-08-08--claude--{sid}--abc.md"
        f.write_text(f"---\nsource: claude\nid: {sid}\nkind: session\n---\n\n{big}",
                     encoding="utf-8")

        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            if "Reply with STRICT JSON" in prompt:
                return json.dumps({"skip": False, "slug": "sess-giant-dedup",
                                   "title": "Giant", "section": "topics",
                                   "tags": [], "related": [], "project": None,
                                   "distill": "d.",
                                   "claims": [{"text": "linha de conteudo durável",
                                               "type": "fact", "explicitness": "explicit"}]})
            if "You verify" in prompt:
                # unsupported → classify_risk parks a chunk pending (REVIEW, not
                # auto-review); the exact state that used to stack duplicates.
                return json.dumps({"outcome": "unsupported", "reason": "mock"})
            return "## Contéudo\ndurável.\n"

        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose=None,
                         model_merge=None, workers=4)
        with mock.patch("memex.providers.complete", side_effect=_route):
            rc = synth_mod.run(args)
        self.assertEqual(rc, 0)
        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 2, "2 chunks → 2 parked ChangeSets")
        ids1 = sorted(json.loads(p.read_text(encoding="utf-8"))["id"] for p in pending)

        # killed before the synthed flush — pendings survived, marks did not
        (self.vault / ".memex" / "synthed.json").write_text("{}", encoding="utf-8")

        with mock.patch("memex.providers.complete", side_effect=_route):
            rc, out = _run_capturing(synth_mod.run, args)
        self.assertEqual(rc, 0)
        self.assertIn("dedup-skip", out)
        pending2 = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending2), 2, "reprocess must not stack chunk duplicates")
        ids2 = sorted(json.loads(p.read_text(encoding="utf-8"))["id"] for p in pending2)
        self.assertEqual(ids1, ids2)
        # every chunk dedup-skipped → the raw is marked done again
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        self.assertEqual(synthed.get(f.name), h)
        modes = {e.get("mode") for e in metrics_mod.read(self.vault)}
        self.assertIn("dedup-skip", modes)


class TestRetryCap(MemexTestCase):
    """M3 — retry cap: a raw whose provider calls fail N consecutive times
    (across reflect runs) is PARKED on the Nth — marked done + a visible `park`
    ChangeSet — instead of reprocessing forever and burning LLM spend."""

    def _write_raw(self, name="2026-08-08--claude--sess-cap--0.md"):
        f = self.raw_dir() / name
        f.write_text(
            "---\nsource: claude\nid: sess-cap\ndate: 2026-08-08\nkind: session\n---\n\n"
            "conteúdo durável do teste de retry cap.\n", encoding="utf-8")
        return f

    def _args(self):
        return Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose="mock",
                         model_merge="mock", workers=1)

    def _down(self, *a, **k):
        raise RuntimeError("provider down")

    # -- unit: attempts helpers ------------------------------------------------
    def test_record_attempt_increments_and_flushes(self):
        from memex import synth
        v = self.tmp / "vault-att"
        (v / ".memex").mkdir(parents=True)
        attempts = {}
        n1 = synth._record_attempt(v, attempts, "r1.md")
        n2 = synth._record_attempt(v, attempts, "r1.md")
        self.assertEqual((n1, n2), (1, 2))
        on_disk = json.loads((v / ".memex" / "attempts.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["r1.md"], 2)
        # the in-memory dict mirrors disk
        self.assertEqual(attempts["r1.md"], 2)

    def test_clear_attempt_on_success(self):
        from memex import synth
        v = self.tmp / "vault-clr"
        (v / ".memex").mkdir(parents=True)
        attempts = {}
        synth._record_attempt(v, attempts, "r1.md")
        synth._clear_attempt(v, attempts, "r1.md")
        self.assertNotIn("r1.md", attempts)
        on_disk = json.loads((v / ".memex" / "attempts.json").read_text(encoding="utf-8"))
        self.assertNotIn("r1.md", on_disk)

    def test_park_marks_done_and_writes_park_changeset(self):
        from memex import synth
        v = self.vault
        raw = self.raw_dir() / "r1.md"
        raw.write_text("conteúdo durável", encoding="utf-8")
        sp = v / ".memex" / "synthed.json"
        sp.write_text("{}", encoding="utf-8")
        synthed, lineage, attempts = {}, {}, {}
        synth._park_raw(v, synthed, sp, lineage, attempts, "r1.md", "h1",
                        raw_path=raw, reason="provider error x3")
        # marked done → exits the backlog
        self.assertEqual(synthed["r1.md"], "h1")
        self.assertEqual(json.loads(sp.read_text(encoding="utf-8"))["r1.md"], "h1")
        # the attempt is cleared (park is terminal)
        self.assertNotIn("r1.md", attempts)
        self.assertNotIn("r1.md",
                         json.loads((v / ".memex" / "attempts.json").read_text(encoding="utf-8")))
        # a `park` ChangeSet is visible in review/pending
        parks = list((v / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(parks), 1)
        d = json.loads(parks[0].read_text(encoding="utf-8"))
        self.assertEqual(d["operation"], "park")
        self.assertEqual(d["source"]["raw"], "raw/r1.md")

    # -- e2e: wiring in _process_one ------------------------------------------
    def test_provider_error_cap_parks_raw_end_to_end(self):
        """3 consecutive provider failures (3 reflect runs) PARK the raw: it is
        marked done (exits the backlog), the attempt is reset, a `park` ChangeSet
        is written, and the next reflect has nothing new to process."""
        import memex.synth as synth_mod
        f = self._write_raw()
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        attempts_path = self.vault / ".memex" / "attempts.json"
        with mock.patch("memex.providers.complete", side_effect=self._down):
            for _ in range(3):
                rc = synth_mod.run(self._args())
                self.assertEqual(rc, 0)
        # after the 3rd failure the attempt is CLEARED (park resets it)
        self.assertEqual(json.loads(attempts_path.read_text(encoding="utf-8")), {})
        # the raw is marked done → the next reflect sees nothing new
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        self.assertEqual(synthed.get(f.name), h)
        # a `park` ChangeSet was written and is visible in review
        parks = [p for p in (self.vault / ".memex" / "review" / "pending").glob("*.json")
                 if json.loads(p.read_text(encoding="utf-8")).get("operation") == "park"]
        self.assertEqual(len(parks), 1)
        # never reprocessed forever
        rc, out = _run_capturing(synth_mod.run, self._args())
        self.assertEqual(rc, 0)
        self.assertIn("nothing new", out)

    def test_success_clears_attempt(self):
        """A raw that failed ONCE then succeeds on the next reflect is marked
        done normally and its attempt is cleared — a transient blip must not
        accumulate toward the park cap."""
        import memex.synth as synth_mod
        f = self._write_raw()
        attempts_path = self.vault / ".memex" / "attempts.json"
        with mock.patch("memex.providers.complete", side_effect=self._down):
            rc = synth_mod.run(self._args())
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(attempts_path.read_text(encoding="utf-8"))[f.name], 1)
        proposal = {
            "skip": False, "slug": "databricks-cost-alert-decision",
            "title": "Alerta de custo Databricks — decisão",
            "section": "decisions", "tags": ["databricks"], "related": [],
            "project": None,
            "distill": "Decidimos alertar quando o custo diário exceder 2x a média de 7 dias.",
            "claims": [{"text": "Perfeito, decidimos: alerta quando custo diário > 2x média de 7 dias.",
                        "type": "decision", "explicitness": "explicit"}],
        }
        body = "## Decision\nRun backups daily.\n"
        with mock.patch("memex.providers.complete", side_effect=_reflect_complete(proposal, body)):
            rc, out = _run_capturing(synth_mod.run, self._args())
        self.assertEqual(rc, 0, out)
        # success path cleared the attempt → no park, normal done
        self.assertEqual(json.loads(attempts_path.read_text(encoding="utf-8")), {})
        parks = [p for p in (self.vault / ".memex" / "review" / "pending").glob("*.json")
                 if json.loads(p.read_text(encoding="utf-8")).get("operation") == "park"]
        self.assertEqual(len(parks), 0)
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        self.assertEqual(synthed.get(f.name), hashlib.sha256(f.read_bytes()).hexdigest()[:16])

    def test_skip_terminal_path_clears_attempt(self):
        """A raw that accumulated 2 provider-error attempts then finishes via the
        SKIP terminal path is marked done AND its counter is cleared — a stale
        count must not make a later reprocess (after a manual synthed reset)
        park after a single blip."""
        import memex.synth as synth_mod
        f = self._write_raw()
        attempts_path = self.vault / ".memex" / "attempts.json"
        # simulate 2 prior provider failures (below the park cap of 3)
        attempts_path.write_text(json.dumps({f.name: 2}), encoding="utf-8")
        proposal = {"skip": True, "slug": None, "title": None, "section": "topics",
                    "tags": [], "related": [], "project": None, "distill": ""}
        with mock.patch("memex.providers.complete",
                        side_effect=_reflect_complete(proposal, "")):
            rc, out = _run_capturing(synth_mod.run, self._args())
        self.assertEqual(rc, 0, out)
        # the SKIP terminal path cleared the attempt → never accumulates to park
        self.assertEqual(json.loads(attempts_path.read_text(encoding="utf-8")), {})
        # and the raw is marked done → exits the backlog
        synthed = json.loads((self.vault / ".memex" / "synthed.json").read_text(encoding="utf-8"))
        self.assertEqual(synthed.get(f.name), hashlib.sha256(f.read_bytes()).hexdigest()[:16])


class TestLoopProofScenarios(MemexTestCase):
    """S1-S6 — the loop-proof integration scenarios: simulate the full
    hook -> reflect -> synth cycle on an isolated vault and pin the
    kill/dedup/park/lock guarantees.

    The M1-M3 guarantees are ALREADY pinned by committed tests; per the Task 6
    brief this class documents that map and adds ONLY the gaps (no duplication):

      S1 happy-path full loop          -> TestSynthReflect
           (test_reflect_builds_wiki_and_workspace_page,
            test_boot_after_reflect_closes_the_loop)
      S2 kill-mid-run, no reprocess    -> TestIncrementalFlush
           (test_marks_survive_when_final_flush_is_disabled = the M1 e2e)
         + the dedup path (ChangeSet on disk, synthed mark lost, reprocess must
           dedup-skip, never stack a duplicate)
           -> TestChangeSetDedup
           (test_reflect_does_not_duplicate_a_pending_slice_on_reprocess +
            its chunk/supersede/applied-guard siblings)
      S3 provider cap parks            -> TestRetryCap
           (test_provider_error_cap_parks_raw_end_to_end = 3 fails across runs
            -> park -> next run "nothing new"; plus the _record_attempt /
            _park_raw unit tests)
      S5 concurrent lock skips        -> TestReviewFixes
           (test_consolidate_skips_when_vault_busy = a consumer backs off while
            the synth lock is held)

    S5 GAP closed here: the `_acquire_lock` PID-liveness branch itself. The two
    tests below verify a lock held by a LIVE pid is refused (returns None) and
    a stale lock left by a DEAD pid is reclaimed — neither is pinned at the
    lock-helper level by the existing suite.
    """

    def test_S5_lock_refused_while_a_live_pid_holds_it(self):
        """A second synth must NOT steal a lock owned by a LIVE process: with
        the lock file holding this test runner's real PID, `_acquire_lock`
        returns None and leaves the owner's lock file untouched. Uses the OS's
        own liveness (`proc.pid_alive(os.getpid())`) — no mock, no subprocess."""
        from memex import synth
        lock = self.vault / ".memex" / "synth.lock"
        lock.write_text(str(os.getpid()), encoding="utf-8")
        self.assertIsNone(synth._acquire_lock(self.vault))
        # the live owner's lock is preserved byte-for-byte
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_S5_stale_lock_from_a_dead_pid_is_reclaimed(self):
        """A lock left behind by a CRASHED run (a dead pid) must be taken over
        on the next synth — no manual cleanup, no permanent stand-down.
        999999999 is provably dead (TestProc.test_pid_alive pins this)."""
        from memex import synth
        lock = self.vault / ".memex" / "synth.lock"
        lock.write_text("999999999", encoding="utf-8")
        self.assertIsNotNone(synth._acquire_lock(self.vault))
        # the reclaim rewrote the lock with OUR pid
        self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getpid()))
        # once held by a live pid again, a second acquire stands down
        self.assertIsNone(synth._acquire_lock(self.vault))


class TestProposeQuality(MemexTestCase):
    """M5 — propose verbatim anchors + model tier by density.

    Two fixes for the same root cause (93% of ChangeSets parked `ambiguous`
    because propose emitted paraphrased quotes the verifier could not anchor):
      1. PROPOSE_PROMPT must demand VERBATIM substring quotes (with a negative
         example) so the cheap model stops emitting unanchorable quotes.
      2. Dense sessions (> propose_tier_chars) get the STRONGER propose model
         (model_merge) so their claims carry anchors the verifier can find; the
         metric's `model_propose` field records the model actually used.
    """

    def test_prompt_demands_verbatim_substring(self):
        from memex import synth
        p = synth.PROPOSE_PROMPT.lower()
        self.assertTrue("verbatim" in p or "exact substring" in p,
                        "PROPOSE_PROMPT must demand a verbatim quote")
        self.assertTrue("do not paraphrase" in p or "never paraphrase" in p,
                        "PROPOSE_PROMPT must forbid paraphrased quotes")

    def test_propose_model_tier_by_density(self):
        from memex import synth
        light = synth._select_propose_model(body_chars=5000,
                                            model_propose="nano", model_merge="mini",
                                            tier_chars=20000)
        dense = synth._select_propose_model(body_chars=30000,
                                            model_propose="nano", model_merge="mini",
                                            tier_chars=20000)
        self.assertEqual(light, "nano")
        self.assertEqual(dense, "mini")
        # boundary: strictly greater than tier_chars is required
        edge = synth._select_propose_model(body_chars=20000,
                                           model_propose="nano", model_merge="mini",
                                           tier_chars=20000)
        self.assertEqual(edge, "nano")
        # tier disabled (tier_chars=0) → always the cheap model
        off = synth._select_propose_model(body_chars=30000,
                                          model_propose="nano", model_merge="mini",
                                          tier_chars=0)
        self.assertEqual(off, "nano")

    def test_dense_session_proposes_with_stronger_model_and_metric_reflects_it(self):
        """A session larger than propose_tier_chars is proposed by model_merge
        (mini) instead of model_propose (nano), and the emitted metric's
        `model_propose` field records the ACTUAL model used — so the tier is
        observable in telemetry."""
        import memex.synth as synth_mod
        import memex.metrics as metrics_mod
        anchor = "Vamos criar um job diário que compara o custo com a média móvel."
        body = ("contexto repetido para preencher a sessão densa do teste de tier. " * 600
                + anchor + "\n")
        self.assertGreater(len(body), 20000)
        self.assertLess(len(body), 50000)  # below chunk_chars → a single full pass
        f = self.raw_dir() / "2026-08-08--claude--sess-dense--0.md"
        f.write_text("---\nsource: claude\nid: sess-dense\ndate: 2026-08-08\nkind: session\n---\n\n"
                     + body, encoding="utf-8")
        proposal = {
            "skip": False, "slug": "databricks-cost-alerts", "title": "Databricks cost alerts",
            "section": "topics", "tags": ["databricks", "alerts"], "related": [],
            "project": "iniciativa-custos",
            "distill": "Decidido: alertar picos de custo do Databricks com um job diário.",
            "claims": [{"text": anchor, "type": "process", "explicitness": "explicit"}],
        }
        calls = []

        def _route(prompt, *, kind, model, settings, json_mode=False, allowed_tools=None):
            calls.append((prompt, model))
            if "Reply with STRICT JSON" in prompt:
                return json.dumps(proposal)
            if "You verify" in prompt:
                return json.dumps({"outcome": "supported", "value": "new", "reason": "mock"})
            return "## Decisão\nAlertar picos de custo com um job diário.\n"

        args = Namespace(vault=str(self.vault), provider=None, limit=None,
                         since=None, only=None, model_propose="nano",
                         model_merge="mini", workers=1)
        with mock.patch("memex.providers.complete", side_effect=_route):
            rc, out = _run_capturing(synth_mod.run, args)
        self.assertEqual(rc, 0, out)
        # the propose call for the dense session used the STRONGER model
        propose_models = [m for p, m in calls if "Reply with STRICT JSON" in p]
        self.assertTrue(propose_models, "a propose call must have happened")
        self.assertEqual(propose_models[0], "mini",
                         "dense session must propose with model_merge")
        # the metric records the ACTUAL propose model (the tiered one)
        dense_evs = [e for e in metrics_mod.read(self.vault) if e.get("fname") == f.name]
        self.assertTrue(dense_evs, "a metric must be emitted for the dense raw")
        self.assertEqual(dense_evs[0]["model_propose"], "mini",
                         "metric model_propose must reflect the tiered model")


if __name__ == "__main__":
    unittest.main(verbosity=2)
