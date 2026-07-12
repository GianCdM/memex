"""memex v2 test suite — stdlib only, no real LLM, no network beyond localhost.

Covers the whole brain loop in-process:
  vault ensure/upgrade · capture (hook payload -> raw) · synth (mock provider)
  · reflect (wiki + now-page) · boot (SessionStart injection) · recall
  (ranking + session dedup) · handoff hold · hook install/uninstall · search
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
from argparse import Namespace
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memex import boot as boot_mod          # noqa: E402
from memex import capture as capture_mod    # noqa: E402
from memex import config as config_mod      # noqa: E402
from memex import hook as hook_mod          # noqa: E402
from memex import ingest as ingest_mod      # noqa: E402
from memex import now as now_mod            # noqa: E402
from memex import proc                      # noqa: E402
from memex import recall as recall_mod      # noqa: E402
from memex import reflect as reflect_mod    # noqa: E402
from memex import scrub as scrub_mod        # noqa: E402
from memex import search as search_mod      # noqa: E402
from memex import synth as synth_mod        # noqa: E402
from memex import vault as vault_mod        # noqa: E402


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
            })
        elif "WORKING-MEMORY" in prompt:                # now-page generation
            content = ("## Contexto\nAlertas de custo do Databricks.\n\n"
                       "## Estado atual\nJob diário criado e testado.\n\n"
                       "## Próximos passos\n- [ ] ligar o schedule\n\n"
                       "## Arquivos-chave\n- jobs/cost_alert.py — o job\n")
        elif "consolidating several wiki pages" in prompt:  # tidy merge
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
        now_mod._PROJECT_CACHE.clear()

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
        return now_mod.project_key(str(self.workspace))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class TestVault(MemexTestCase):
    def test_ensure_creates_v2_layout(self):
        for rel in ("raw", "now", "wiki/topics", "wiki/decisions", "wiki/projects",
                    ".memex/state", "SCHEMA.md", "index.md", "log.md"):
            self.assertTrue((self.vault / rel).exists(), rel)
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
        self.assertTrue((v1 / "now").is_dir())
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

    def test_full_capture_supersedes_partial_note(self):
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
        raws = list((self.vault / "raw").glob("*.md"))
        self.assertEqual(len(raws), 1)             # same file superseded
        self.assertIn("Slack", raws[0].read_text(encoding="utf-8"))


class TestRecall(MemexTestCase):
    PAGES = [
        {"slug": "databricks-cost-alerts", "title": "Databricks cost alerts",
         "section": "topics", "tier": "silver", "tags": ["databricks", "alerts"],
         "summary": "Daily job alerting on Databricks cost spikes",
         "path": "topics/databricks-cost-alerts.md", "project": "ws"},
        {"slug": "airflow-migration", "title": "Airflow migration",
         "section": "topics", "tier": "silver", "tags": ["airflow"],
         "summary": "Plan to migrate DAGs to Airflow 3",
         "path": "topics/airflow-migration.md", "project": "ws"},
    ]

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


class TestBoot(MemexTestCase):
    def test_boot_injects_now_page_and_usage(self):
        proj = self.project()
        now_mod.write_now(self.vault, proj, "## Contexto\nAlertas Databricks.\n"
                          "## Próximos passos\n- [ ] ligar schedule", author="handoff")
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

    def test_boot_ignores_stale_now_page(self):
        proj = self.project()
        p = now_mod.write_now(self.vault, proj, "## Contexto\nvelho", author="auto")
        old = p.read_text(encoding="utf-8").replace(
            re.search(r"updated: (\S+)", p.read_text(encoding="utf-8")).group(1),
            "2020-01-01T00:00:00Z")
        p.write_text(old, encoding="utf-8")
        _, out = _run_capturing(boot_mod.run, Namespace(vault=str(self.vault)),
                                payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertNotIn("velho", out)


class TestBriefing(MemexTestCase):
    def _write_briefing(self, text="## Hoje\n- 1:1 com a Ana às 10h\n- review do OKR Q3"):
        old = os.getcwd()
        os.chdir(self.workspace)
        try:
            rc, out = _run_capturing(
                now_mod.briefing_cmd,
                Namespace(vault=str(self.vault), project=None, show=False,
                          text=text, stdin=False))
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0, out)

    def test_boot_injects_fresh_briefing(self):
        self._write_briefing()
        rc, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace), "session_id": "b1"})
        self.assertEqual(rc, 0)
        self.assertIn("Today's briefing", out)
        self.assertIn("1:1 com a Ana", out)

    def test_boot_drops_stale_briefing(self):
        self._write_briefing()
        key = now_mod.briefing_key(self.project())
        p = now_mod.now_path(self.vault, key)
        text = p.read_text(encoding="utf-8")
        stamp = re.search(r"updated: (\S+)", text).group(1)
        p.write_text(text.replace(stamp, "2020-01-01T00:00:00Z"), encoding="utf-8")
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace)})
        self.assertNotIn("1:1 com a Ana", out)

    def test_briefing_and_handoff_coexist_in_boot(self):
        self._write_briefing()
        now_mod.write_now(self.vault, self.project(),
                          "## Contexto\nrefactor do recall", author="handoff")
        _, out = _run_capturing(
            boot_mod.run, Namespace(vault=str(self.vault)),
            payload={"source": "startup", "cwd": str(self.workspace), "session_id": "b2"})
        self.assertIn("Where we left off", out)
        self.assertIn("Today's briefing", out)
        self.assertIn("refactor do recall", out)
        self.assertIn("1:1 com a Ana", out)


class TestNowHandoff(MemexTestCase):
    def test_handoff_roundtrip_and_hold(self):
        proj = self.project()
        old_cwd = os.getcwd()
        os.chdir(self.workspace)
        try:
            rc, out = _run_capturing(
                now_mod.handoff_cmd,
                Namespace(vault=str(self.vault), project=None, show=False,
                          text="## Contexto\nSalvando estado.", stdin=False))
        finally:
            os.chdir(old_cwd)
        self.assertEqual(rc, 0)
        meta, body = now_mod.read_now(self.vault, proj)
        self.assertEqual(meta.get("author"), "handoff")
        self.assertIn("Salvando estado", body)
        self.assertTrue(now_mod.hold_active(self.vault, proj, hold_hours=12))
        self.assertFalse(now_mod.hold_active(self.vault, proj, hold_hours=0))
        # log.md got a line
        self.assertIn(f"now/{proj}", (self.vault / "log.md").read_text(encoding="utf-8"))


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

    def test_reflect_builds_wiki_and_now_page(self):
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
        meta, body = now_mod.read_now(self.vault, self.project())
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
        _, body = now_mod.read_now(self.vault, "notas")
        self.assertIn("Próximos passos", body or "")

    def test_auto_tidy_runs_on_cadence(self):
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
                          "tier": "silver", "tags": ["dedup"], "sources": [],
                          "summary": "dedup de pedidos", "path": path, "project": "ws"})
        self.seed_index(pages)
        rc, out = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=None, since=None, limit=None,
                      provider=None))
        self.assertEqual(rc, 0, out)
        self.assertIn("auto-tidy", out)
        idx = json.loads((self.vault / ".memex" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(idx["pages"]), 1)                     # merged into one
        self.assertTrue(list((self.vault / ".memex" / "history" / "gardening").glob("*.md")),
                        "absorbed page was not archived")
        # cadence: a second reflect right away must NOT tidy again
        rc, out2 = _run_capturing(
            reflect_mod.run,
            Namespace(vault=str(self.vault), cwd=None, since=None, limit=None,
                      provider=None))
        self.assertNotIn("auto-tidy", out2)

    def test_reflect_respects_fresh_handoff(self):
        self._capture_session("sess-llm2")
        now_mod.write_now(self.vault, self.project(),
                          "## Contexto\nMEU handoff manual.", author="handoff")
        _run_capturing(reflect_mod.run,
                       Namespace(vault=str(self.vault), cwd=str(self.workspace),
                                 since=None, limit=None, provider=None))
        _, body = now_mod.read_now(self.vault, self.project())
        self.assertIn("MEU handoff manual", body)  # not clobbered

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


class TestAuditFixes(MemexTestCase):
    def test_tidy_archives_the_canonical_page_too(self):
        """'Recoverable, never hard-lost' must hold for canon — it gets
        OVERWRITTEN by a merge that saw truncated bodies."""
        from memex import gardening
        pages = []
        for suffix in ("", "-v2"):
            slug = f"pipeline-vendas-dedup{suffix}"
            path = f"topics/{slug}.md"
            (self.vault / "wiki" / path).write_text(
                f"---\ntitle: \"{slug}\"\n---\n\n## Original de {slug}\nconteúdo íntegro\n",
                encoding="utf-8")
            pages.append({"slug": slug, "title": slug, "section": "topics",
                          "tier": "silver", "tags": [], "sources": [],
                          "summary": "dedup", "path": path, "project": "ws"})
        self.seed_index(pages)
        srv, base = _start_mock_llm()
        try:
            cfg = config_mod.load_global()
            cfg["provider"] = {"order": ["openai_compat"],
                               "openai_compat": {"base_url": base, "api_key": None,
                                                 "model_propose": "mock", "model_merge": "mock"}}
            config_mod.save_global(cfg)
            rc, out = _run_capturing(lambda a: gardening.consolidate(self.vault), None)
        finally:
            srv.shutdown()
        self.assertEqual(rc, 0, out)
        archived = list((self.vault / ".memex" / "history" / "gardening").glob("*.md"))
        names = " ".join(a.name for a in archived)
        self.assertIn("pipeline-vendas-dedup-v2", names)      # absorbed sibling
        self.assertIn("--pipeline-vendas-dedup.md", names)    # canon itself
        canon_archive = [a for a in archived if a.name.endswith("--pipeline-vendas-dedup.md")]
        self.assertIn("conteúdo íntegro", canon_archive[0].read_text(encoding="utf-8"))

    def test_handoff_workspace_path_stays_inside_the_vault(self):
        """--workspace <absolute path> must not Path-join its way OUT of the vault."""
        rc, _ = _run_capturing(
            now_mod.handoff_cmd,
            Namespace(vault=str(self.vault), project=str(self.workspace),
                      show=False, text="## Contexto\nvia path", stdin=False))
        self.assertEqual(rc, 0)
        page = self.vault / "now" / "ws.md"                   # repo-name key, inside vault
        self.assertTrue(page.exists())
        self.assertIn("via path", page.read_text(encoding="utf-8"))

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
        args = Namespace(vault=str(self.vault), tier_override=None,
                         docs=str(self.workspace), exclude=None)
        with redirect_stdout(io.StringIO()):
            ingest_mod._ingest_docs(self.vault, args, ingest_mod._ledger_load(self.vault))
        raw_docs = lambda: list((self.vault / "raw").glob("*--doc--*.md"))  # noqa: E731
        self.assertEqual(len(raw_docs()), 0)
        orig = ingest_mod.extract_mod.extract     # "markitdown got installed"
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
        args = lambda: Namespace(vault=str(self.vault), tier_override=None,  # noqa: E731
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
        _, plan = hook_mod._install(self.workspace, self.vault)
        self.assertEqual(set(plan), {"SessionStart", "UserPromptSubmit",
                                     "SessionEnd", "PreCompact"})
        cfg = json.loads((self.workspace / ".claude" / "settings.local.json")
                         .read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
