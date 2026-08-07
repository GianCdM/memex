# Wiki Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `wiki/` a source-backed, current-only canonical graph by adding a fail-closed ChangeSet promotion pipeline, moving operational artifacts outside the graph, and repairing the existing wiki through reversible audit lots.

**Architecture:** Add a small canonical-index module that all read paths share, then move deterministic generated views out of `wiki/`. Build ChangeSets, validation, transactions, history manifests, link rewrites, review commands, and health reporting as separate stdlib-only modules; route LLM synthesis through this proposal/promoter boundary. Repair existing content only after the new invariants, rollback path, and dry-run auditor are shipped.

**Tech Stack:** Python 3.10+ standard library; `argparse`; JSON/JSONL; Markdown/YAML-frontmatter parsing already in `memex.synth`; existing LLM provider abstraction; existing newline-delimited MCP server; `unittest`.

## Global Constraints

- Keep `raw/` immutable; never delete raw source material.
- `workspace/` remains automatic, short-lived working memory and is outside canonical wiki promotion.
- `wiki/` contains only current canonical pages under `topics/`, `entities/`, and `decisions/`.
- Generated views, audits, review proposals, transactions, and archived history live under `.memex/` and never enter normal recall, embeddings, or the default graph.
- Use only the Python standard library at runtime; do not add dependencies.
- All canonical writes are serialized under the existing `synth._acquire_lock(vault)` lock; validate source and target hashes while holding that lock.
- Ambiguous or high-impact changes fail closed into review; verifier failures never publish automatically.
- Never automatically publish an entity, decision, person/team/ownership statement, commitment, deadline, preference, sensitive fact, conflict, or semantic reclassification.
- Decision pages are never archived or absorbed automatically; they are superseded or restored only.
- Use correct Portuguese accents in all user-facing Portuguese text.
- Do not mention or add dependencies on external session-reconstruction skills.
- Do not run a real-vault mutation until the dry-run migration task and rollback tests pass and the user explicitly approves the proposed lot.
- Commit each completed task with the stated commit message and include `Co-Authored-By: Claude <noreply@anthropic.com>` in each commit message.

---

## File and responsibility map

| Path | Responsibility |
|---|---|
| `memex/canon.py` | Canonical page predicate, canonical index filtering/rebuild, file/body hashing, semantic identity validation, and canonical-path validation. |
| `memex/views.py` | Deterministic brain index and project hub generation under `.memex/views/`; no canonical page mutation. |
| `memex/gardening.py` | Candidate duplicate detection only; writes suggestions to `.memex/audit/`; no direct LLM merge after migration. |
| `memex/changes.py` | ChangeSet schema, durable state store, anchors, deterministic validation, transaction journal, history manifests, promotion, archive, merge, link rewrite, and rollback. |
| `memex/verify.py` | Proposal prompt, independent fidelity-verifier prompt, supported/partial/unsupported/conflicting outcomes, and risk classification. |
| `memex/audit.py` | Read-only health scan, generated-artifact migration ChangeSets, technical identity scan, duplicate scan, evidence lookup, lot selection, and Markdown/JSON reporting. |
| `memex/review.py` | CLI-facing listing/show/approve/reject/edit/rollback presentation around `changes.py`. |
| `memex/synth.py` | Produce structured ChangeSets rather than direct canonical page writes; remove forced/decorative link policy; regenerate index/views through canonical helpers. |
| `memex/reflect.py` | Replace auto-tidy merge with candidate scan/audit and run embeddings only after canonical promotion. |
| `memex/recall.py`, `memex/search.py`, `memex/embed.py`, `memex/relink.py` | Consume the canonical page set only. |
| `memex/cli.py` | Add `audit`, `health`, and `review` commands; update status wording and suggestions path. |
| `memex/mcp_server.py` | Add structured health/review operations and update `remember` so it reports ChangeSet state rather than direct wiki publication. |
| `memex/remember.py` | Preserve immediate raw capture but create and process a ChangeSet instead of inline direct synthesis. |
| `memex/analyze.py` | Emit code-origin ChangeSets or explicitly mark code pages as review-required; do not bypass promoter. |
| `memex/vault.py` | Create the new `.memex/` subdirectories and refresh the shipped schema to document current-only wiki and review flow. |
| `memex/limits.py` | Add only behavioral limits required by candidate verification/audit sampling; do not add cosmetic constants. |
| `tests/test_memex.py` | Extend the existing stdlib suite with helper fixtures and regression tests for every task. |
| `tests/live_e2e.sh` | Update the mock end-to-end loop after the direct-synthesis contract is replaced. |
| `README.md` | Update the public architecture, generated-view locations, review/audit commands, and graph contract. |

## Dependency order and decisions locked by this plan

1. Build canonical filtering before archive/history migration so archived content cannot leak into recall, embeddings, graph, or search.
2. Define stable page/raw hash semantics before ChangeSets. Page hash is SHA-256 over the body after tool-owned frontmatter is stripped and normalized; raw hash is SHA-256 over the complete immutable raw file. Do not use the changing `updated:` field as a ChangeSet precondition.
3. Version changed raw captures by content hash instead of overwriting a raw file in place. A newer capture creates a new raw artifact and the ledger records the content identity; old raw remains immutable.
4. Move generated views atomically: writers and every reader change in the same task.
5. Decisions are supersede-or-rollback only. They never participate in automatic archive or duplicate absorption.
6. `remember` remains immediately captured into `raw/`, but its ChangeSet follows normal risk policy. A low-risk explicit topic may auto-promote; an entity/decision/preference remains pending review. Update existing tests that assume immediate canonical publication.
7. Archive/reject never changes `synthed.json`. Instead, a ChangeSet records the raw hash it evaluated; resynthesis of an archived/rejected source requires an explicit `memex review retry <id>` command in a later enhancement, not implicit reprocessing.
8. Code analysis is not raw-backed session evidence. Preserve `kind: code` architecture pages only through an explicit future code-evidence adapter; this implementation first routes `memex analyze` outputs to pending review rather than attempting a false raw-anchor validation.
9. Use filesystem snapshot (`tar` or copy) for real-vault migration verification because `.memex/` is ignored by vault Git settings.

---

### Task 1: Canonical page primitives and read-path filtering

**Files:**
- Create: `memex/canon.py`
- Modify: `memex/recall.py:254-328`
- Modify: `memex/search.py:24-59`
- Modify: `memex/embed.py:93-227`
- Modify: `memex/relink.py:40-177`
- Modify: `memex/mcp_server.py:79-99`
- Test: `tests/test_memex.py`

**Interfaces:**
- Produces `canonical_pages(vault: Path, index: dict | None = None) -> list[dict]`.
- Produces `is_canonical_record(vault: Path, page: dict) -> bool`.
- Produces `canonical_path(vault: Path, page: dict) -> Path | None`.
- Produces `load_index(vault: Path) -> dict` and `write_index(vault: Path, pages: list[dict]) -> None`.
- Later tasks consume these functions instead of reading all `index.json` entries directly.

- [ ] **Step 1: Write failing canonical-filter tests**

Add imports near the existing module imports:

```python
from memex import canon as canon_mod
from memex import embed as embed_mod
```

Add this test class after `TestRecall`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestCanonicalPages -v
```

Expected: FAIL with `ImportError` because `memex.canon` does not exist.

- [ ] **Step 3: Implement `memex/canon.py`**

Create:

```python
"""Canonical wiki-page predicates and index helpers.

Only current pages stored under wiki/topics, wiki/entities, and wiki/decisions
are part of the recallable graph. Generated views, history, drafts, stale index
records, and non-current lifecycle entries are never canonical.
"""

from __future__ import annotations

import json
from pathlib import Path

CANONICAL_SECTIONS = frozenset({"topics", "entities", "decisions"})


def load_index(vault: Path) -> dict:
    try:
        data = json.loads((Path(vault) / ".memex" / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pages": []}
    return data if isinstance(data, dict) else {"pages": []}


def write_index(vault: Path, pages: list[dict]) -> None:
    path = Path(vault) / ".memex" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pages": pages}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical_path(vault: Path, page: dict) -> Path | None:
    section = page.get("section")
    rel = page.get("path")
    if section not in CANONICAL_SECTIONS or not isinstance(rel, str):
        return None
    candidate = (Path(vault) / "wiki" / rel).resolve()
    root = (Path(vault) / "wiki" / section).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def is_canonical_record(vault: Path, page: dict) -> bool:
    if page.get("status") != "current":
        return False
    path = canonical_path(vault, page)
    return path is not None and path.is_file()


def canonical_pages(vault: Path, index: dict | None = None) -> list[dict]:
    data = index if index is not None else load_index(vault)
    return [page for page in data.get("pages", []) if is_canonical_record(vault, page)]
```

- [ ] **Step 4: Wire all read paths to canonical pages**

Make these exact replacements:

```python
# memex/recall.py
from . import canon as canon_mod
# Replace:
pages = index.get("pages", [])
# With:
pages = canon_mod.canonical_pages(vault, index)
```

```python
# memex/search.py
from . import canon as canon_mod
# Replace the `index.get("pages", [])` argument in hybrid_rank with:
canon_mod.canonical_pages(vault, index)
```

```python
# memex/embed.py
from . import canon as canon_mod
# Replace:
pages = idx.get("pages", [])
# With:
pages = canon_mod.canonical_pages(vault, idx)
```

```python
# memex/mcp_server.py
from . import canon as canon_mod
# Replace the `index.get("pages", [])` argument in _tool_search with:
canon_mod.canonical_pages(vault, index)
```

In `memex/relink.py`, call `canon_mod.canonical_pages(vault, idx)` before passing pages to `_build_graph`; do not make graph links from noncanonical index records.

- [ ] **Step 5: Add embedding exclusion test and implement its smallest fix**

Add to `TestCanonicalPages`:

```python
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
```

Run:

```bash
python -m unittest tests.test_memex.TestCanonicalPages -v
```

Expected: PASS.

- [ ] **Step 6: Run full regression suite**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: PASS. Fix any fixture that deliberately uses legacy `kind: silver` only if the failure is caused by the new canonical filter; preserve test intent.

- [ ] **Step 7: Commit**

```bash
git add memex/canon.py memex/recall.py memex/search.py memex/embed.py memex/relink.py memex/mcp_server.py tests/test_memex.py
git commit -m "feat: filter memory reads to canonical wiki pages

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Stable hashes and immutable raw versions

**Files:**
- Modify: `memex/canon.py`
- Modify: `memex/ingest.py:57-127`
- Modify: `memex/capture.py`
- Modify: `memex/synth.py:123-157,329-357`
- Modify: `tests/test_memex.py`

**Interfaces:**
- Produces `page_body_hash(text: str) -> str` in `memex.canon`.
- Produces `file_hash(path: Path) -> str` in `memex.canon`.
- Produces raw filenames that include a content-hash suffix and never overwrite prior raw evidence.
- Later ChangeSets use `raw_sha256` from `file_hash(raw_path)` and `expected_page_sha256` from `page_body_hash(page_text)`.

- [ ] **Step 1: Write failing hash and raw-immutability tests**

Add to `TestCanonicalPages`:

```python
def test_page_body_hash_ignores_tool_owned_updated_frontmatter(self):
    before = "---\ntitle: \"Topic\"\nupdated: 2026-08-01\n---\n\n## Rule\nKeep evidence.\n"
    after = "---\ntitle: \"Topic\"\nupdated: 2026-08-06\n---\n\n## Rule\nKeep evidence.\n"
    self.assertEqual(canon_mod.page_body_hash(before), canon_mod.page_body_hash(after))
```

Add to `TestCapture`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_memex.TestCanonicalPages.test_page_body_hash_ignores_tool_owned_updated_frontmatter \
  tests.test_memex.TestCapture.test_changed_capture_preserves_prior_raw_evidence -v
```

Expected: FAIL because `page_body_hash` does not exist and current capture overwrites/supersedes one raw file.

- [ ] **Step 3: Implement stable hash helpers**

Append to `memex/canon.py`:

```python
import hashlib
from . import synth as synth_mod


def page_body_hash(text: str) -> str:
    """Hash only canonical body content, excluding tool-owned frontmatter."""
    _, body = synth_mod._read_frontmatter(text)
    normalized = "\n".join(line.rstrip() for line in body.strip().splitlines()) + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
```

Avoid circular imports by moving `_read_frontmatter` to a tiny neutral helper if importing `synth` causes a cycle during test collection. The final dependency direction must be `synth -> canon` or `canon -> format helper`, never `canon -> synth -> canon`.

- [ ] **Step 4: Version raw filenames by content identity**

Replace `_write_raw` filename construction in `memex/ingest.py` with this implementation shape:

```python
content_hash = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
short_hash = content_hash[:12]
fname = f"{datepart}--{source}--{_slugify(sid, 32)}--{short_hash}.md"
```

Before writing, if the resulting path already exists, return its name without rewriting it. Add this raw frontmatter field:

```python
f"content_sha256: {content_hash}\n"
```

Keep the source ID and ledger identity. Update any capture code that assumes one raw file per source ID to select the newest raw by date/mtime rather than mutating an old file.

- [ ] **Step 5: Update old overwrite assumptions**

Replace `TestCapture.test_full_capture_supersedes_partial_note` with a test asserting both partial and final captures persist as separate raw evidence versions and that workspace selection chooses the newest final capture. Update code in `memex/capture.py` and `memex/workspace.py` only as required to make that selection deterministic.

Use this expectation:

```python
self.assertEqual(len(raws), 2)
self.assertTrue(any("Slack" not in raw.read_text(encoding="utf-8") for raw in raws))
self.assertTrue(any("Slack" in raw.read_text(encoding="utf-8") for raw in raws))
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python -m unittest tests.test_memex.TestCapture tests.test_memex.TestCanonicalPages -v
```

Expected: PASS.

- [ ] **Step 7: Run the full suite and commit**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: PASS.

Commit:

```bash
git add memex/canon.py memex/ingest.py memex/capture.py memex/workspace.py tests/test_memex.py
git commit -m "feat: preserve immutable raw versions and stable content hashes

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Move generated views and suggestions outside the canonical wiki

**Files:**
- Create: `memex/views.py`
- Modify: `memex/synth.py:671-757`
- Modify: `memex/gardening.py:288-333`
- Modify: `memex/analyze.py:217-240`
- Modify: `memex/vault.py:17-95,156-235`
- Modify: `memex/boot.py`
- Modify: `memex/search.py:47-49`
- Modify: `memex/cli.py:79-113`
- Modify: `memex/mcp_server.py:123-146`
- Modify: `memex/skill.py:63-64`
- Modify: `README.md`
- Test: `tests/test_memex.py`

**Interfaces:**
- Produces `write_views(vault: Path, index: dict) -> None`.
- Produces `write_merge_suggestions(vault: Path, clusters: list[list[dict]]) -> int`.
- Removes all writes to `index.md`, `wiki/projects/`, and `wiki/_sugestoes.md`.

- [ ] **Step 1: Write failing generated-artifact tests**

Add:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestGeneratedViews -v
```

Expected: FAIL because writers still use root `index.md`, `wiki/projects`, and `wiki/_sugestoes.md`.

- [ ] **Step 3: Implement `memex/views.py`**

Create a module that owns all generated Markdown. It must write:

```text
.memex/views/brain-index.md
.memex/views/projects-index.md
.memex/views/projects/<project>.md
```

Reuse current text content from `synth._write_index_md` and `_write_project_hubs`, but remove YAML `kind: hub` frontmatter because views are not wiki pages. Export:

```python
def write_views(vault: Path, index: dict) -> None:
    ...
```

The project-view writer must clear stale generated `*.md` files inside `.memex/views/projects/` before regenerating, while preserving no user-authored files because that directory is machine-owned. `.memex/` is already a dot-prefixed operational folder; do not create or modify `.obsidian/` configuration. The application documentation may recommend Obsidian's built-in excluded-files setting as an optional UI preference, but correctness cannot rely on it.

- [ ] **Step 4: Redirect every writer and reader atomically**

Make these changes in the same commit:

```python
# memex/synth.py
# Replace `_write_index_md` body with:
from . import views as views_mod
views_mod.write_views(vault, idx)
```

```python
# memex/gardening.py
SUGGESTIONS_FILE = "merge-suggestions.md"
# Change output directory from vault / "wiki" to vault / ".memex" / "audit".
```

Update all UI text and paths:

- `cli._status_cmd`: count `.memex/audit/merge-suggestions.md` and print `audit suggestions`.
- `mcp_server._tool_status`: use the same path and key name `suggestions`.
- `search.run`: point an empty-search user to `.memex/views/brain-index.md`.
- `boot.py` and `skill.py`: point navigation guidance to `.memex/views/brain-index.md` and `.memex/views/projects/<project>.md`.
- `analyze.py`: stop writing project hubs itself; call `views.write_views` after index updates.

Change `vault.ensure` to create:

```python
for d in ("history", "state", "review/pending", "review/applying", "review/applied",
          "review/rejected", "review/stale", "audit", "views/projects", "manifests"):
```

Do not create `wiki/projects` for new vaults. Leave it untouched in existing vaults; Task 8 migrates its files through ChangeSets.

- [ ] **Step 5: Update schema and public documentation**

Replace the generated-artifact parts of `SCHEMA_TEMPLATE` with:

```text
- `wiki/` — current canonical semantic memory only: topics, entities, decisions.
- `.memex/views/` — regenerated catalogs and project navigation, not knowledge.
- `.memex/audit/` — health reports and duplicate candidates, not knowledge.
- `.memex/history/` — machine-managed prior page revisions, outside normal recall and graph.
```

Update README diagram and layer table to show views/audit under `.memex/`, and remove claims that `wiki/projects/` or root `index.md` are durable wiki pages.

- [ ] **Step 6: Run focused and full tests**

Run:

```bash
python -m unittest tests.test_memex.TestGeneratedViews -v
python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add memex/views.py memex/synth.py memex/gardening.py memex/analyze.py memex/vault.py memex/boot.py memex/search.py memex/cli.py memex/mcp_server.py memex/skill.py README.md tests/test_memex.py
git commit -m "feat: move generated wiki views outside canonical graph

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: ChangeSet store, structural gate, transaction journal, and promoter

**Files:**
- Create: `memex/changes.py`
- Modify: `memex/canon.py`
- Modify: `memex/vault.py`
- Modify: `memex/cli.py`
- Test: `tests/test_memex.py`

**Interfaces:**
- Produces `new_changeset(...) -> dict`.
- Produces `save_changeset(vault: Path, change: dict) -> Path`.
- Produces `load_changeset(vault: Path, change_id: str) -> tuple[dict, Path]`.
- Produces `validate_structure(vault: Path, change: dict) -> list[str]`.
- Produces `apply_changeset(vault: Path, change_id: str, *, approved: bool = False) -> dict`.
- Produces `rollback_changeset(vault: Path, change_id: str) -> dict`.
- Produces `rewrite_incoming_links(vault: Path, old_slug: str, new_slug: str, pages: list[dict]) -> dict[str, str]`.
- Creates `.memex/transactions.jsonl` and `.memex/history/manifests/<id>.json`.

- [ ] **Step 1: Write failing ChangeSet-store tests**

Add imports:

```python
from memex import changes as changes_mod
```

Add:

```python
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
        changes_mod.save_changeset(self.vault, change)
        path.write_text(path.read_text(encoding="utf-8") + "\nChanged concurrently.\n", encoding="utf-8")

        result = changes_mod.apply_changeset(self.vault, change["id"])

        self.assertEqual(result["state"], "stale")
        self.assertIn("Changed concurrently.", path.read_text(encoding="utf-8"))

    def test_apply_and_rollback_restore_page_and_transaction(self):
        page, path = self._current_page()
        change = self._repair_change(page, path)
        changes_mod.save_changeset(self.vault, change)

        applied = changes_mod.apply_changeset(self.vault, change["id"])
        self.assertEqual(applied["state"], "applied")
        self.assertIn("New value.", path.read_text(encoding="utf-8"))
        self.assertTrue((self.vault / ".memex" / "transactions.jsonl").exists())

        rolled_back = changes_mod.rollback_changeset(self.vault, change["id"])
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertIn("Old value.", path.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestChangeSets -v
```

Expected: FAIL with `ImportError` because `memex.changes` does not exist.

- [ ] **Step 3: Implement ChangeSet persistence and structural validation**

Create `memex/changes.py` with these exact foundational pieces:

```python
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from . import canon as canon_mod
from . import synth

_STATES = frozenset({"pending", "applying", "applied", "rejected", "stale", "rolled_back"})
_OPERATIONS = frozenset({"create", "update", "merge", "reclassify", "archive", "repair"})
_TECHNICAL_IDENTITY = re.compile(r"^(?:note-[a-f0-9]{6,}|untitled|misc|draft|doc)(?:-|$)", re.I)


def _review_dir(vault: Path, state: str) -> Path:
    if state not in _STATES:
        raise ValueError(f"unknown ChangeSet state: {state}")
    path = Path(vault) / ".memex" / "review" / state
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_changeset(*, operation, classification, source, target, claims, proposed_body, risk, reason):
    if operation not in _OPERATIONS:
        raise ValueError(f"unknown ChangeSet operation: {operation}")
    return {
        "id": uuid.uuid4().hex,
        "state": "pending",
        "operation": operation,
        "created_at": int(time.time()),
        "classification": dict(classification),
        "source": dict(source),
        "target": dict(target),
        "claims": list(claims),
        "proposed_body": proposed_body,
        "risk": risk,
        "reason": reason,
        "verification": {},
    }


def save_changeset(vault: Path, change: dict) -> Path:
    state = change.get("state", "pending")
    path = _review_dir(vault, state) / f"{change['id']}.json"
    path.write_text(json.dumps(change, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_changeset(vault: Path, change_id: str):
    for state in _STATES:
        path = Path(vault) / ".memex" / "review" / state / f"{change_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), path
    raise FileNotFoundError(f"unknown ChangeSet: {change_id}")


def _semantic_identity(classification: dict) -> bool:
    slug = str(classification.get("slug") or "")
    title = str(classification.get("title") or "")
    return bool(slug and title and not _TECHNICAL_IDENTITY.match(slug) and "/" not in slug and "://" not in title)


def validate_structure(vault: Path, change: dict) -> list[str]:
    errors = []
    if change.get("operation") not in _OPERATIONS:
        errors.append("unknown operation")
    if not _semantic_identity(change.get("classification") or {}):
        errors.append("classification must have a semantic title and slug")
    raw_rel = (change.get("source") or {}).get("raw")
    raw_path = Path(vault) / str(raw_rel or "")
    if not raw_path.is_file():
        errors.append("source raw file is missing")
    elif canon_mod.file_hash(raw_path) != (change.get("source") or {}).get("raw_sha256"):
        errors.append("source raw hash does not match")
    section = (change.get("classification") or {}).get("section")
    if section not in canon_mod.CANONICAL_SECTIONS:
        errors.append("classification section is not canonical")
    return errors
```

- [ ] **Step 4: Implement locked apply, transaction snapshot, and rollback**

Complete `changes.py` with this behavior:

1. Acquire `synth._acquire_lock(vault)` and return `{"state": "pending", "error": "vault busy"}` if unavailable.
2. Load proposal, set state to `applying`, and move its JSON file from its prior state directory.
3. Re-run `validate_structure` under the lock.
4. Resolve target from canonical index. For a `repair`/`update`, read its page and compare `canon_mod.page_body_hash(text)` to `expected_page_sha256`; on mismatch move ChangeSet to `stale` and return `{"state": "stale"}`.
5. Read `change["verification"]["route"]`. If it is `review` and `approved` is false, leave the ChangeSet pending and return `{"state": "pending", "reason": "explicit approval required"}`. If it is `archive` or `reject`, do not apply a normal update.
6. Write a JSON transaction snapshot before mutation with `before_files`, `after_files`, `index_before`, and `link_rewrites`.
7. Render the target using `synth._render_page` with preserved tool-owned metadata and `proposed_body`.
8. Write the new page, rebuild the canonical index through `canon_mod.write_index`, call `views.write_views`, append a JSONL transaction event, and move ChangeSet to `applied`.
9. For rollback, read the transaction snapshot, restore every `before_files` byte-for-byte, restore `index_before`, rebuild views, append a rollback event, and set ChangeSet state to `rolled_back`.

Keep all writes in `Path.replace`/temporary-file style so a crash cannot leave a half-written JSON or Markdown file. Add helper `_atomic_write(path: Path, text: str) -> None` using `path.with_suffix(path.suffix + ".tmp")` then `replace`.

- [ ] **Step 5: Run tests to verify the implementation passes**

Run:

```bash
python -m unittest tests.test_memex.TestChangeSets -v
```

Expected: PASS.

- [ ] **Step 6: Add CLI skeleton for review and health dispatch**

In `memex/cli.py`, add parser declarations without audit logic yet:

```python
preview = sub.add_parser("review", help="inspect and apply pending wiki changes")
preview.add_argument("action", nargs="?", default="list", choices=["list", "show", "approve", "reject", "rollback"])
preview.add_argument("change_id", nargs="?")
preview.add_argument("--reason")
preview.add_argument("--vault")
preview.set_defaults(func=review_mod.run)

phealth = sub.add_parser("health", help="report canonical wiki integrity")
phealth.add_argument("--vault")
phealth.set_defaults(func=audit_mod.health_run)
```

Do not wire these parsers until Tasks 5 and 7 create `review_mod` and `audit_mod`; keep this code change in the task that adds those imports to avoid broken imports.

- [ ] **Step 7: Run full suite and commit**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: PASS.

Commit:

```bash
git add memex/changes.py memex/canon.py memex/vault.py tests/test_memex.py
git commit -m "feat: add reversible changeset promotion primitives

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Evidence anchors, fidelity verification, and risk routing

**Files:**
- Create: `memex/verify.py`
- Modify: `memex/changes.py`
- Modify: `memex/limits.py`
- Test: `tests/test_memex.py`

**Interfaces:**
- Produces `validate_evidence(vault: Path, change: dict) -> list[dict]`.
- Produces `verify_fidelity(vault: Path, change: dict, provider: str | None = None) -> dict`.
- Produces `classify_risk(change: dict, evidence: list[dict], fidelity: dict) -> str` returning `auto_apply`, `review`, `archive`, or `reject`.
- `changes.apply_changeset` consumes verification outcomes and only applies `auto_apply` or an explicit approval after all gates pass.

- [ ] **Step 1: Write failing evidence/risk tests**

Add:

```python
from memex import verify as verify_mod
```

Add:

```python
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
                "evidence": [{"raw": "raw/source.md", "raw_sha256": canon_mod.file_hash(raw), "start_line": 5, "end_line": 5, "quote": quote}],
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestVerification -v
```

Expected: FAIL with `ImportError` because `memex.verify` does not exist.

- [ ] **Step 3: Implement deterministic evidence validation**

Create `memex/verify.py`:

```python
"""Evidence and risk gates for ChangeSet promotion."""

from __future__ import annotations

import json
from pathlib import Path

from . import canon as canon_mod

_HIGH_IMPACT_TERMS = frozenset({"owner", "ownership", "responsável", "prazo", "deadline", "commitment", "compromisso", "preference", "preferência"})


def validate_evidence(vault: Path, change: dict) -> list[dict]:
    outcomes = []
    for claim in change.get("claims", []):
        anchors = claim.get("evidence") or []
        claim_outcome = "unsupported"
        for anchor in anchors:
            path = Path(vault) / str(anchor.get("raw") or "")
            quote = str(anchor.get("quote") or "")
            if not path.is_file() or not quote:
                continue
            if canon_mod.file_hash(path) != anchor.get("raw_sha256"):
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            start = max(1, int(anchor.get("start_line") or 1))
            end = min(len(lines), int(anchor.get("end_line") or start))
            excerpt = "\n".join(lines[start - 1:end])
            if quote in excerpt:
                claim_outcome = "supported"
                break
        outcomes.append({"claim": claim.get("text", ""), "outcome": claim_outcome})
    return outcomes


def classify_risk(change: dict, evidence: list[dict], fidelity: dict) -> str:
    if any(item.get("outcome") != "supported" for item in evidence):
        return "archive"
    if fidelity.get("outcome") != "supported":
        return "review"
    section = (change.get("classification") or {}).get("section")
    if section in {"entities", "decisions"} or change.get("operation") in {"reclassify", "merge", "archive"}:
        return "review"
    text = " ".join(str(c.get("text", "")) for c in change.get("claims", [])).lower()
    if any(term in text for term in _HIGH_IMPACT_TERMS):
        return "review"
    return "auto_apply"
```

- [ ] **Step 4: Add independent verifier prompt and fail-closed provider call**

Add to `verify.py`:

```python
FIDELITY_PROMPT = """You verify whether a proposed wiki update is faithful to explicit source evidence.
Return STRICT JSON only:
{"outcome":"supported|partial|unsupported|conflicting","reason":"short explanation"}

SOURCE EVIDENCE:
{evidence}

CURRENT PAGE:
{current}

PROPOSED BODY:
{proposed}
"""


def verify_fidelity(vault: Path, change: dict, *, kind: str, model: str, settings: dict) -> dict:
    from . import providers
    evidence = json.dumps(validate_evidence(vault, change), ensure_ascii=False)
    try:
        response = providers.complete(
            FIDELITY_PROMPT.format(evidence=evidence, current="", proposed=change.get("proposed_body", "")),
            kind=kind,
            model=model,
            settings=settings,
            json_mode=True,
        )
        parsed = json.loads(response)
    except Exception as exc:
        return {"outcome": "ambiguous", "reason": f"verifier unavailable: {type(exc).__name__}"}
    outcome = parsed.get("outcome")
    if outcome not in {"supported", "partial", "unsupported", "conflicting"}:
        return {"outcome": "ambiguous", "reason": "invalid verifier response"}
    return {"outcome": outcome, "reason": str(parsed.get("reason") or "")}
```

Use the merge model for the verifier initially, but as a separate completion. Add a per-vault `limits["verify_timeout_seconds"]` only if provider timeout separation is needed; otherwise reuse the existing provider timeout and keep limits unchanged.

- [ ] **Step 5: Integrate verification in ChangeSet application**

In `changes.apply_changeset`, before any write under the lock:

1. Call `validate_evidence`.
2. Read `change["verification"]`; if it has no successful fidelity result, do not call a provider inside `apply_changeset` yet. Move the proposal to `pending` with `verification.outcome = "required"` and return `{"state": "pending", "reason": "fidelity verification required"}`.
3. After Task 6 wires proposal generation, it will populate `verification`. For now, make a manually seeded `verification={"outcome": "supported"}` eligible for test-only repairs.
4. Call `classify_risk`; auto-apply only when it returns `auto_apply`. All other outcomes remain pending/review except demonstrably unsupported existing topic pages, which Task 7 archives through an explicit archive ChangeSet.

- [ ] **Step 6: Run tests**

Run:

```bash
python -m unittest tests.test_memex.TestVerification tests.test_memex.TestChangeSets -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add memex/verify.py memex/changes.py memex/limits.py tests/test_memex.py
git commit -m "feat: validate changeset evidence and promotion risk

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Review CLI, health report, and MCP review interface

**Files:**
- Create: `memex/review.py`
- Create: `memex/audit.py`
- Modify: `memex/cli.py`
- Modify: `memex/mcp_server.py`
- Modify: `memex/vault.py`
- Test: `tests/test_memex.py`

**Interfaces:**
- Produces `review.run(args) -> int`.
- Produces `audit.health(vault: Path) -> dict` and `audit.health_run(args) -> int`.
- Produces MCP tools `health`, `review_list`, `review_show`, `review_approve`, `review_reject`, and `review_rollback`.

- [ ] **Step 1: Write failing review and health tests**

Add:

```python
from memex import audit as audit_mod
from memex import review as review_mod
```

Add:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestReviewAndHealth -v
```

Expected: FAIL because `audit` and `review` modules do not exist.

- [ ] **Step 3: Implement health report**

Create `memex/audit.py` with:

```python
from __future__ import annotations

import json
from pathlib import Path

from . import canon as canon_mod


def _review_count(vault: Path, state: str = "pending") -> int:
    return len(list((Path(vault) / ".memex" / "review" / state).glob("*.json")))


def health(vault: Path) -> dict:
    pages = canon_mod.canonical_pages(vault)
    invalid = [p for p in pages if not p.get("slug") or p["slug"].startswith("note-")]
    report = {
        "canonical_pages": len(pages),
        "by_section": {},
        "pending_reviews": _review_count(vault),
        "stale_reviews": _review_count(vault, "stale"),
        "invalid_current_identities": len(invalid),
        "dead_links": 0,
        "suggestions": 0,
    }
    for page in pages:
        section = page.get("section", "topics")
        report["by_section"][section] = report["by_section"].get(section, 0) + 1
    return report


def write_health_report(vault: Path, report: dict) -> None:
    audit_dir = Path(vault) / ".memex" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Memex health", "", f"- Canonical pages: {report['canonical_pages']}", f"- Pending review: {report['pending_reviews']}", f"- Stale review: {report['stale_reviews']}", f"- Invalid current identities: {report['invalid_current_identities']}"]
    (audit_dir / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def health_run(args) -> int:
    from . import config as config_mod
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    report = health(vault)
    write_health_report(vault, report)
    print(f"wiki: {report['canonical_pages']} current · {report['pending_reviews']} in review · {report['invalid_current_identities']} invalid identities")
    return 0
```

- [ ] **Step 4: Implement review lifecycle commands**

Create `memex/review.py` with `run(args)` dispatching:

```python
list     -> print pending ChangeSets with id, operation, target/classification, risk, reason
show     -> print full JSON for one ChangeSet
approve  -> call changes.apply_changeset(..., approved=True) and print state/reason
reject   -> call changes.transition_changeset(vault, id, "rejected", reason=args.reason)
rollback -> call changes.rollback_changeset and print state/reason
```

Add `transition_changeset(vault, change_id, new_state, reason=None) -> dict` in `changes.py`. It must move the JSON between state directories atomically and append `review_reason` and `updated_at`.

- [ ] **Step 5: Wire CLI and MCP**

In `memex/cli.py`:

```python
from . import audit as audit_mod
from . import review as review_mod
```

Add these parsers after `pstat`:

```python
phealth = sub.add_parser("health", help="report canonical wiki integrity")
phealth.add_argument("--vault")
phealth.set_defaults(func=audit_mod.health_run)

preview = sub.add_parser("review", help="inspect and apply pending wiki changes")
preview.add_argument("action", nargs="?", default="list", choices=["list", "show", "approve", "reject", "rollback"])
preview.add_argument("change_id", nargs="?")
preview.add_argument("--reason")
preview.add_argument("--vault")
preview.set_defaults(func=review_mod.run)
```

In `memex/mcp_server.py`, add tool schemas and dispatch functions with only structured return values. At minimum add:

```text
health(vault?)
review_list(vault?, state?)
review_show(change_id, vault?)
review_approve(change_id, vault?)
review_reject(change_id, reason, vault?)
review_rollback(change_id, vault?)
```

Each handler resolves a vault, calls the corresponding module, and returns dicts. Preserve stdout protocol-only behavior.

- [ ] **Step 6: Update status output**

Replace `wiki pages` in both CLI and MCP status with counts based on `canon_mod.canonical_pages`. Count suggestions from `.memex/audit/merge-suggestions.md`. Add `pending_reviews` to MCP status.

- [ ] **Step 7: Run focused, MCP, and full tests**

Run:

```bash
python -m unittest tests.test_memex.TestReviewAndHealth tests.test_memex.TestMcpServer -v
python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add memex/audit.py memex/review.py memex/changes.py memex/cli.py memex/mcp_server.py memex/vault.py tests/test_memex.py
git commit -m "feat: add wiki health and review interfaces

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Route synth, remember, and reflect through ChangeSets

**Files:**
- Modify: `memex/synth.py`
- Modify: `memex/remember.py`
- Modify: `memex/analyze.py:217-252,311-363`
- Modify: `memex/mcp_server.py:102-120`
- Modify: `memex/reflect.py:38-142`
- Modify: `memex/gardening.py:128-285`
- Modify: `memex/limits.py`
- Modify: `tests/test_memex.py`
- Modify: `tests/live_e2e.sh`

**Interfaces:**
- `synth.run` creates ChangeSets and invokes verification/risk routing; it does not directly write canonical pages.
- `remember.run` reports the ChangeSet ID/state.
- `reflect.run` builds proposals, refreshes workspace, writes duplicate suggestions, and embeds only after successfully applied canonical mutations.
- `gardening.consolidate` becomes a compatibility wrapper that creates duplicate-merge ChangeSets instead of direct LLM merges.

- [ ] **Step 1: Write failing synthesis-routing tests**

Add to `TestSynthReflect`:

```python
def test_reflect_creates_pending_changeset_for_decision_instead_of_writing_wiki(self):
    self._capture_session("decision-review")
    proposal = {
        "skip": False,
        "slug": "backup-decision",
        "title": "Backup decision",
        "section": "decisions",
        "tags": ["backup"],
        "related": [],
        "project": None,
        "distill": "Decided to run backups daily.",
        "claims": [{"text": "Decided to run backups daily.", "type": "decision", "explicitness": "explicit"}],
    }
    with mock.patch("memex.providers.complete", side_effect=[json.dumps(proposal), "## Decision\nRun backups daily.\n", json.dumps({"outcome": "supported", "reason": "explicit"})]):
        rc, out = _run_capturing(reflect_mod.run, Namespace(vault=str(self.vault), cwd=str(self.workspace), since=None, limit=None, provider=None, workers=1))

    self.assertEqual(rc, 0, out)
    self.assertFalse((self.vault / "wiki" / "decisions" / "backup-decision.md").exists())
    pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
    self.assertEqual(len(pending), 1)
    self.assertEqual(json.loads(pending[0].read_text(encoding="utf-8"))["classification"]["section"], "decisions")


def test_reflect_auto_applies_supported_low_risk_topic(self):
    self._capture_session("topic-auto")
    proposal = {
        "skip": False,
        "slug": "daily-backup-runbook",
        "title": "Daily backup runbook",
        "section": "topics",
        "tags": ["backup"],
        "related": [],
        "project": None,
        "distill": "The runbook requires a daily backup.",
        "claims": [{"text": "The runbook requires a daily backup.", "type": "process", "explicitness": "explicit"}],
    }
    with mock.patch("memex.providers.complete", side_effect=[json.dumps(proposal), "## Rule\nThe runbook requires a daily backup.\n", json.dumps({"outcome": "supported", "reason": "explicit"})]):
        rc, out = _run_capturing(reflect_mod.run, Namespace(vault=str(self.vault), cwd=str(self.workspace), since=None, limit=None, provider=None, workers=1))

    self.assertEqual(rc, 0, out)
    self.assertTrue((self.vault / "wiki" / "topics" / "daily-backup-runbook.md").exists())
    self.assertEqual(list((self.vault / ".memex" / "review" / "pending").glob("*.json")), [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_memex.TestSynthReflect.test_reflect_creates_pending_changeset_for_decision_instead_of_writing_wiki \
  tests.test_memex.TestSynthReflect.test_reflect_auto_applies_supported_low_risk_topic -v
```

Expected: FAIL because current synth writes `wiki/` directly and proposal JSON does not include claim anchors.

- [ ] **Step 3: Extend proposal schema and prompts for claims with anchors**

Replace the strict JSON example in `PROPOSE_PROMPT` with a schema that requires `claims` and source line references:

```text
"claims": [{
  "text": "one narrow durable statement",
  "type": "process|fact|decision|entity|commitment",
  "explicitness": "explicit|inferred",
  "evidence": [{"start_line": 1, "end_line": 1, "quote": "exact text copied from RAW NOTE"}]
}]
```

Add rules:

```text
- Every durable claim MUST have an exact quote copied from RAW NOTE and line range.
- If you cannot name a semantic title and slug, return "skip": true with no fallback identity.
- New entities and decisions are valid proposals but will be reviewed; do not downgrade them to topics.
- Return only links that are explicitly relevant to the source; zero links is allowed.
```

Remove the mandatory two-to-six related-link rule and remove `_lexical_related` fallback use from the synth path. Retain `_prune_wikilinks` but make it validate against canonical slugs only.

- [ ] **Step 4: Convert source-relative anchors to absolute raw anchors**

After parsing a proposal, use the raw file content to verify that each returned quote appears exactly once or resolve the first matching line range. Construct anchor dictionaries with:

```python
{
    "raw": f"raw/{f.name}",
    "raw_sha256": canon_mod.file_hash(f),
    "start_line": start_line,
    "end_line": end_line,
    "quote": quote,
}
```

If no claim anchor can be resolved, create a pending ChangeSet with `verification.outcome = "ambiguous"`; never create a `note-*` page.

- [ ] **Step 5: Replace direct synth page writes with ChangeSet routing**

In `_process_one`, retain raw selection, proposer call, body merge call, and existing-page lookup. Replace code from the current direct write block beginning at `# ── phase 3: write` with:

1. Build a ChangeSet using the semantic title/slug/section, source raw hash, current target body hash if target exists, normalized anchors, proposed body, and initial risk.
2. Call `verify.validate_evidence` and the independent `verify.verify_fidelity` completion.
3. Store results in `change["verification"]`.
4. Route using `verify.classify_risk`:
   - `auto_apply`: save then call `changes.apply_changeset`.
   - `review`: save pending.
   - `archive`/`reject`: save pending with outcome reason; do not mutate a new page.
5. Mark the raw synthesized in `synthed.json` only after the ChangeSet has been durably saved, regardless of whether it is applied or pending.
6. Rebuild views/index only after a promoter applies a canonical mutation.

Update printed messages so they distinguish:

```text
raw -> applied ChangeSet <id> -> wiki/topics/<slug>.md
raw -> pending ChangeSet <id> (review required)
raw -> skipped (no durable knowledge)
```

- [ ] **Step 6: Change `remember` and MCP remember output**

In `memex/remember.py`, keep raw capture, then call `synth.run` for `only=fname`. Read ChangeSets whose source raw is `raw/<fname>` and print:

```text
✓ saved -> raw/<fname>
  change: <id> (<state>)
```

In `mcp_server._tool_remember`, return:

```python
{"ok": True, "file": f"raw/{fname}", "changes": [{"id": ..., "state": ...}]}
```

Do not return a boolean named `synthesized`, because canonical publication is no longer equivalent to processing.

- [ ] **Step 7: Route code analysis to review without false raw provenance**

In `memex/analyze.py`, keep repository scanning and digest generation unchanged, but replace `_write_pages` direct writes with one pending ChangeSet per generated architecture page. Use a code-source payload:

```python
source={
    "kind": "code",
    "repo": str(root),
    "git_ref": _git_head(root),
    "digest_sha256": hashlib.sha256(digest.encode("utf-8")).hexdigest(),
}
```

Add `_git_head(root) -> str | None` using `git -C <root> rev-parse HEAD` with the existing `proc.run_kwargs` error handling. Classify every code ChangeSet as `risk: "review"` and set `verification={"outcome": "code_evidence_required", "route": "review"}`. Do not call raw-anchor validation for this source kind. This preserves the code-analysis feature while preventing direct writes that bypass the canonical promoter.

Add a test that `memex analyze` creates a pending code ChangeSet with `kind: code` and leaves `wiki/topics/` unchanged.

- [ ] **Step 8: Replace destructive automatic tidy**

Modify `reflect._auto_tidy` to call `gardening.write_suggestions(vault)` only. Do not call `gardening.consolidate` automatically.

Modify `gardening.consolidate` so it detects clusters and creates pending duplicate-merge ChangeSets rather than calling a merge LLM or deleting files. For now, mechanical auto-merge is handled in Task 8 audit lots; manual `memex tidy` becomes an alias for candidate generation and prints the number of ChangeSets created.

Replace tests asserting automatic direct merge (`test_auto_tidy_runs_on_cadence`, `test_tidy_archives_the_canonical_page_too`) with tests asserting a duplicate candidate appears in `.memex/review/pending/` and canonical page files remain unchanged.

- [ ] **Step 9: Run focused and full tests**

Run:

```bash
python -m unittest tests.test_memex.TestSynthReflect tests.test_memex.TestAuditFixes tests.test_memex.TestMcpServer -v
python -m unittest discover -s tests -v
bash tests/live_e2e.sh
```

Expected: all unit tests pass; the live script must be updated so a supported topic expects a ChangeSet with `state: applied`, while a decision expects `state: pending`.

- [ ] **Step 10: Commit**

```bash
git add memex/synth.py memex/remember.py memex/mcp_server.py memex/reflect.py memex/gardening.py memex/limits.py tests/test_memex.py tests/live_e2e.sh
git commit -m "feat: route wiki synthesis through verified changesets

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Archive, merge, backlink rewrite, and recovery-safe history manifests

**Files:**
- Modify: `memex/changes.py`
- Modify: `memex/canon.py`
- Modify: `memex/audit.py`
- Test: `tests/test_memex.py`

**Interfaces:**
- Produces `archive_changeset(vault: Path, change_id: str) -> dict`.
- Produces `merge_changeset(vault: Path, change_id: str) -> dict`.
- Produces `rewrite_incoming_links(...) -> dict[str, str]`.
- Produces `.memex/history/wiki/<relative-canonical-path>` and `.memex/history/manifests/<change-id>.json`.

- [ ] **Step 1: Write failing archive/merge tests**

Add:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestArchiveAndMerge -v
```

Expected: FAIL because promoter only implements repair/update.

- [ ] **Step 3: Implement archive behavior**

Extend `apply_changeset` for `operation == "archive"`:

1. Reject if target section is `decisions` with message `decision pages must be superseded, not archived`.
2. Read the canonical target, create a history copy at `.memex/history/wiki/<target.path>`.
3. Update history copy frontmatter to `status: archived` and append an archive note with the ChangeSet reason. Preserve all source fields/body.
4. Remove the visible `wiki/` file.
5. Remove the target record from the canonical index.
6. Rebuild views and transaction snapshot.

- [ ] **Step 4: Implement merge and backlink rewrite**

Extend `apply_changeset` for `operation == "merge"`:

1. Require `origins` with at least one distinct current canonical topic slug.
2. Reject origins in `decisions` or an origin/target section mismatch.
3. Before changing anything, scan canonical page bodies for `[[origin-slug]]` and create a mapping to `[[target-slug]]`.
4. Write the consolidated target body.
5. Rewrite and snapshot all incoming-link pages.
6. Move each origin to `.memex/history/wiki/<origin.path>`, set history frontmatter `status: superseded` and `superseded_by: [[target-slug]]`, remove visible origin files/index entries, and write a manifest listing origins and link rewrites.
7. Rebuild views and transaction journal.

Use this exact replacement regex for plain wikilinks:

```python
re.sub(r"\[\[" + re.escape(old_slug) + r"\]\]", f"[[{new_slug}]]", body)
```

Do not rewrite aliased links or arbitrary text in this first increment; record them as review findings in the manifest if detected.

- [ ] **Step 5: Add rollback tests and implementation**

Extend both archive and merge tests to call `changes_mod.rollback_changeset(...)` and assert that original visible page files, index membership, and incoming `[[duplicate-topic]]` link are restored. Implement rollback from transaction snapshots for these operations.

- [ ] **Step 6: Run focused and full tests**

Run:

```bash
python -m unittest tests.test_memex.TestArchiveAndMerge tests.test_memex.TestChangeSets -v
python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add memex/changes.py memex/canon.py memex/audit.py tests/test_memex.py
git commit -m "feat: archive and merge wiki pages with rollback history

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: Dry-run wiki auditor and recovery lots 0–2

**Files:**
- Modify: `memex/audit.py`
- Modify: `memex/cli.py`
- Modify: `memex/mcp_server.py`
- Modify: `memex/vault.py`
- Modify: `tests/test_memex.py`
- Modify: `README.md`

**Interfaces:**
- Produces `audit.run(args) -> int` for `memex audit [--dry-run] [--lot 0|1|2] [--vault PATH]`.
- Produces `scan_generated_artifacts(vault) -> list[dict]`.
- Produces `scan_technical_identities(vault) -> list[dict]`.
- Produces `scan_mechanical_duplicates(vault) -> list[dict]`.
- Produces `.memex/audit/latest.{md,json}` and pending ChangeSets for review/auto-apply candidates.

- [ ] **Step 1: Write failing audit-lot tests**

Add:

```python
class TestAuditLots(MemexTestCase):
    def test_dry_run_lot_zero_finds_generated_artifacts_without_moving_them(self):
        legacy = self.vault / "wiki" / "_sugestoes.md"
        legacy.write_text("# Sugestões\n", encoding="utf-8")
        project = self.vault / "wiki" / "projects" / "legacy-project.md"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text("# Legacy project\n", encoding="utf-8")

        result = audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=0, provider=None))

        self.assertEqual(result, 0)
        self.assertTrue(legacy.exists())
        report = json.loads((self.vault / ".memex" / "audit" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(report["lots"]["0"]["generated_artifacts"], 2)

    def test_lot_one_creates_review_for_note_identity_without_guessing_title(self):
        page = {"slug": "note-12345678", "title": "note-12345678", "section": "topics", "kind": "session", "status": "current", "tags": [], "sources": ["session:x"], "summary": "unknown", "path": "topics/note-12345678.md", "project": None}
        target = self.vault / "wiki" / page["path"]
        target.write_text("---\ntitle: \"note-12345678\"\n---\n\n## Fragment\nNo source anchor.\n", encoding="utf-8")
        self.seed_index([page])

        audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=1, provider=None))

        pending = list((self.vault / ".memex" / "review" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        change = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(change["operation"], "reclassify")
        self.assertEqual(change["risk"], "review")
        self.assertTrue(target.exists())

    def test_lot_two_creates_merge_candidate_for_normalized_title_duplicate(self):
        first = {"slug": "capacity-planning", "title": "Capacity Planning", "section": "topics", "kind": "session", "status": "current", "tags": [], "sources": ["session:a"], "summary": "same", "path": "topics/capacity-planning.md", "project": None}
        second = dict(first, slug="capacity-planning-v2", path="topics/capacity-planning-v2.md")
        for page in (first, second):
            (self.vault / "wiki" / page["path"]).write_text(f"---\ntitle: \"{page['title']}\"\n---\n\n## Same\n", encoding="utf-8")
        self.seed_index([first, second])

        audit_mod.run(Namespace(vault=str(self.vault), dry_run=True, lot=2, provider=None))

        changes = [json.loads(path.read_text(encoding="utf-8")) for path in (self.vault / ".memex" / "review" / "pending").glob("*.json")]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["operation"], "merge")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_memex.TestAuditLots -v
```

Expected: FAIL because `audit.run` and lot scanners do not exist.

- [ ] **Step 3: Implement lot 0 scanner and report**

In `audit.py`, implement `scan_generated_artifacts` for these known legacy paths only:

```text
wiki/_sugestoes.md
wiki/projects/_index.md
wiki/projects/*.md
index.md
```

For `--dry-run`, only report findings. For non-dry-run, create `repair` ChangeSets that copy each artifact to the deterministic `.memex` destination and remove the legacy visible artifact only through the promoter transaction. Do not infer unknown underscore files; list them in `unknown_underscore_files` for review.

`audit.run` writes both reports every time, with a schema like:

```json
{
  "generated_at": 0,
  "dry_run": true,
  "lots": {"0": {"generated_artifacts": 2, "changesets": []}}
}
```

- [ ] **Step 4: Implement lot 1 technical-identity scanner**

Detect a current page when any is true:

```text
slug starts with note-<hex>, untitled, misc, draft, or doc;
title equals slug;
slug/title contains path separators, a URL marker, or prompt-template fragments.
```

For each, create a `reclassify` ChangeSet with `risk: review`; do not invent a replacement title or move a page. Include candidate raw/document sources and a reason. This preserves the user requirement that identity repair must not be LLM-guessed without evidence.

- [ ] **Step 5: Implement lot 2 mechanical-duplicate scanner**

Treat pages as mechanical duplicates only when all are true:

```text
same section == topics;
same normalized title (lowercase alphanumeric only);
no decision/entity page involved;
source sets intersect;
body hashes are equal OR summaries are equal;
one slug is strictly shorter than the other.
```

Create a `merge` ChangeSet targeting the shorter slug with `origins=[longer_slug]`, risk `low`, and a reason listing the deterministic matching signals. Do not apply during `--dry-run`. Non-dry-run calls `changes.apply_changeset` only if the ChangeSet receives an explicit successful `verification` record from deterministic equality; do not invoke LLM.

- [ ] **Step 6: Wire CLI, MCP, schema, and docs**

In `cli.py` add:

```python
paudit = sub.add_parser("audit", help="scan wiki integrity and prepare reversible repairs")
paudit.add_argument("--vault")
paudit.add_argument("--dry-run", action="store_true")
paudit.add_argument("--lot", type=int, choices=[0, 1, 2])
paudit.add_argument("--provider")
paudit.set_defaults(func=audit_mod.run)
```

Add MCP `audit` with `dry_run` and `lot` arguments. The tool must return report JSON and never apply a non-dry-run lot unless the tool caller explicitly sets `dry_run: false`.

Update `SCHEMA_TEMPLATE` and README with:

```text
Run `memex audit --dry-run` before applying a recovery lot.
Generated views and audit output are under `.memex/`, not canonical wiki pages.
```

- [ ] **Step 7: Run focused and full tests**

Run:

```bash
python -m unittest tests.test_memex.TestAuditLots tests.test_memex.TestReviewAndHealth -v
python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add memex/audit.py memex/cli.py memex/mcp_server.py memex/vault.py README.md tests/test_memex.py
git commit -m "feat: audit generated artifacts identities and duplicate candidates

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: Real-vault dry run, approved small lot, and end-to-end verification

**Files:**
- Modify: `tests/live_e2e.sh`
- Modify: `README.md`
- Create: `docs/superpowers/plans/2026-08-06-wiki-integrity-migration-runbook.md`

**Interfaces:**
- No new runtime interfaces.
- Produces a human-run migration runbook with exact snapshot, dry-run, approval, application, health, and rollback commands.

- [ ] **Step 1: Write the migration runbook before touching real data**

Create `docs/superpowers/plans/2026-08-06-wiki-integrity-migration-runbook.md` containing these exact command templates, with placeholders preserved:

```bash
VAULT="/absolute/path/to/vault"
STAMP="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT="${VAULT%/}-before-wiki-integrity-${STAMP}.tar.gz"
tar -C "$(dirname "$VAULT")" -czf "$SNAPSHOT" "$(basename "$VAULT")"

memex health --vault "$VAULT"
memex audit --vault "$VAULT" --dry-run --lot 0
memex audit --vault "$VAULT" --dry-run --lot 1
memex audit --vault "$VAULT" --dry-run --lot 2
```

Document that no `memex audit` command without `--dry-run` is run until a human has inspected the reports and explicitly selected a lot.

- [ ] **Step 2: Update live E2E test for ChangeSet routing**

Modify `tests/live_e2e.sh` so it creates a mock low-risk topic, asserts an `applied` ChangeSet and canonical page, creates a mock decision, asserts a `pending` ChangeSet and no visible decision page, then runs `memex health` and checks the expected current/review counts. Do not run the script against a personal vault; it must keep using its temporary test vault.

- [ ] **Step 3: Run all automated validation**

Run:

```bash
python -m py_compile memex/*.py
python -m unittest discover -s tests -v
bash tests/live_e2e.sh
```

Expected: all commands exit 0.

- [ ] **Step 4: Perform only the read-only real-vault baseline**

After the user explicitly provides a vault path and explicitly authorizes this read-only operation, run:

```bash
memex health --vault "$VAULT"
memex audit --vault "$VAULT" --dry-run --lot 0
memex audit --vault "$VAULT" --dry-run --lot 1
memex audit --vault "$VAULT" --dry-run --lot 2
```

Report counts, ChangeSet IDs, and paths. Do not run a mutation in this step.

- [ ] **Step 5: Request approval for exactly one small recovery lot**

Present the lot 0 or deterministic lot 2 diff, history destinations, and rollback command. Wait for explicit user approval before running any command without `--dry-run`.

- [ ] **Step 6: Apply one approved lot and exercise rollback**

Only after approval:

```bash
memex audit --vault "$VAULT" --lot <approved-lot>
memex health --vault "$VAULT"
memex review rollback <one-change-id> --vault "$VAULT"
memex health --vault "$VAULT"
```

Confirm restored files/index/link state from the transaction manifest. Do not continue to semantic identity or unsupported-content archival lots in the same change window.

- [ ] **Step 7: Commit runbook and docs**

```bash
git add tests/live_e2e.sh README.md docs/superpowers/plans/2026-08-06-wiki-integrity-migration-runbook.md
git commit -m "docs: add wiki integrity migration runbook

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Plan self-review

### Spec coverage

| Spec requirement | Plan task(s) |
|---|---|
| Current-only canonical graph and read paths | 1 |
| Stable hashes and immutable raw evidence | 2 |
| Generated views/audits/history outside graph | 3 |
| ChangeSet lifecycle, transaction, stale protection, rollback | 4, 8 |
| Evidence anchors and independent fidelity verification | 5, 7 |
| Conversation + CLI + MCP review interface | 6 |
| Synthesis and remember no longer bypass promotion policy | 7 |
| Archive/merge/link rewrite/history manifests | 8 |
| Existing-wiki recovery lots and reports | 9, 10 |
| Fail-closed semantics and tests | 1–10 |
| Real-vault snapshot/dry run/approval/rollback | 10 |

### Placeholder scan

The plan contains no unresolved placeholders, deferred implementation markers, or implicit test instructions. Every implementation task names exact files, expected interfaces, commands, and concrete test code.

### Type consistency

The plan consistently uses `ChangeSet`, `canonical_pages`, `page_body_hash`, `file_hash`, `validate_structure`, `validate_evidence`, `verify_fidelity`, `classify_risk`, `apply_changeset`, and `rollback_changeset`. `audit.run`, `audit.health_run`, and `review.run` are the CLI entry points. All later tasks consume these exact names.
