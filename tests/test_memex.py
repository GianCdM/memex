"""memex v2 test suite — stdlib only, no real LLM, no network beyond localhost.

Covers the whole brain loop in-process:
  vault ensure/upgrade · capture (hook payload -> raw) · synth (mock provider)
  · reflect (wiki + workspace-page) · boot (SessionStart injection) · recall
  (ranking + session dedup) · hook install/uninstall · search
  · scrub · proc.pid_alive.

Run:  python -m unittest discover -s tests -v   (from the repo root)
"""

from __future__ import annotations

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
                # no `claims` on purpose: an empty evidence set is trivially
                # supported, exercising the auto-apply path in the mock
            })
        elif "You verify whether a proposed wiki update" in prompt:  # Task 7 fidelity gate
            content = json.dumps({"outcome": "supported", "reason": "mock"})
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
        if "You verify whether a proposed wiki update" in prompt:
            return json.dumps({"outcome": "supported", "reason": "explicit"})
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


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class TestVault(MemexTestCase):
    def test_ensure_creates_v2_layout(self):
        for rel in ("raw", "workspace", "wiki/topics", "wiki/entities", "wiki/decisions",
                    ".memex/state", ".memex/audit", ".memex/views/projects",
                    ".memex/review/pending", "SCHEMA.md", "log.md"):
            self.assertTrue((self.vault / rel).exists(), rel)
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
        raws = list((self.vault / "raw").glob("*.md"))
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
        self.assertEqual(len(list((self.vault / "raw").glob("*.md"))), 1)
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
        raws = sorted((self.vault / "raw").glob("*.md"))
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

        raws = sorted((self.vault / "raw").glob("*.md"))
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
        raw = self.vault / "raw" / "session.md"
        raw.write_text("---\nsource: claude\nid: s1\ncwd: " + str(self.workspace) + "\n---\n\nprimeiro\n", encoding="utf-8")
        key = self.workspace_key()
        first = workspace_mod.incremental_source(self.vault, key, raw, session_id="s1")
        workspace_mod.write_checkpoint(self.vault, key, first["checkpoint"])
        raw.write_text(raw.read_text(encoding="utf-8") + "segundo\n", encoding="utf-8")
        second = workspace_mod.incremental_source(self.vault, key, raw, session_id="s1")
        self.assertTrue(second["incremental"])
        self.assertEqual(second["delta"], "segundo\n")

    def test_incremental_workspace_cursor_rebuilds_when_prefix_changes(self):
        raw = self.vault / "raw" / "session.md"
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
        raw = self.vault / "raw" / "2026-07-01--claude--legacy--12345678.md"
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
    def _write_raw_session(self, text, date="2026-07-24T12:00:00Z"):
        raw = self.vault / "raw" / "2026-07-24--claude--latest--abc12345.md"
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

        raw = next((self.vault / "raw").glob("*.md"))
        text = raw.read_text(encoding="utf-8").replace(
            "date: 2026-07-24T12:00:00Z", "date: 2020-01-01T00:00:00Z")
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
            "text": "## user\n\nDecisão antiga sobre alertas de custo do Databricks.",
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
        raw_docs = lambda: list((self.vault / "raw").glob("*--doc--*.md"))  # noqa: E731
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
        n_before = len(list((self.vault / "raw").glob("*--doc--*.md")))
        os.utime(doc, (time.time() + 60, time.time() + 60))   # mtime churn
        seen = ingest_mod._ledger_load(self.vault)
        with redirect_stdout(io.StringIO()):
            ingest_mod._ingest_docs(self.vault, args(), seen)
        n_after = len(list((self.vault / "raw").glob("*--doc--*.md")))
        self.assertEqual(n_before, n_after, "mtime churn must not duplicate raw notes")


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
        raw = self.vault / "raw" / "evidence.md"
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
                "raw_sha256": canon_mod.file_hash(self.vault / "raw" / "evidence.md"),
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
        raw = self.vault / "raw" / "source.md"
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
        raw = self.vault / "raw" / "source.md"
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
        raw = self.vault / "raw" / "source.md"
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


class TestVerification(MemexTestCase):
    def _change(self, claim_text, quote, section="topics", operation="create"):
        raw = self.vault / "raw" / "source.md"
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
        self.assertEqual(names, ["search", "remember", "status", "health", "review_list", "review_show", "review_approve", "review_reject", "review_rollback"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
