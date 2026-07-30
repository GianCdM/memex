# Substituir `tier` por `kind` + `status` + changelog — Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir o sistema de tiers (bronze/silver/gold) por `kind` (origem informativa), `status` (vigência da página) e changelog automático por página.

**Architecture:** O `kind` é atribuído na ingestão (capture/ingest/analyze/gardening) e carregado pelo raw note até a wiki page. O `status` é preservado pelo synth e editável manualmente. O changelog é mantido pelo LLM no merge como seção `## 📋 Histórico`. Todo comportamento condicional baseado em tier é removido.

**Tech Stack:** Python 3.10+ stdlib, sem novas dependências.

**Spec:** `docs/superpowers/specs/2026-07-29-kind-and-status-design.md`

## Global Constraints

- `kind` é puramente informativo — zero lógica condicional (`if kind == "X"`)
- `status` default é `"current"`; `superseded` requer `superseded_by`
- Changelog: máx 10 entradas, uma linha por merge, link pro raw note
- `--tier` removido da CLI; tier removido dos prompts do LLM
- Snapshot de gold removido; TIER_RANK removido
- Migração: páginas existentes ganham `kind` inferido dos `sources` + `status: current`
- Testes: 52+ passando com asserts atualizados

---

### Task 1: Atualizar SCHEMA.md e README

**Files:**
- Modify: `memex/vault.py:47-50,64-67`
- Modify: `README.md:139`

**Interfaces:**
- Produces: SCHEMA_TEMPLATE atualizado com `workspace/`, `kind`, `status`, `## 📋 Histórico`

- [ ] **Step 1: Atualizar SCHEMA_TEMPLATE em vault.py**

No `SCHEMA_TEMPLATE` (linha ~47-50), substituir a seção "Trust tiers" por "Page metadata":

```python
## Page metadata
- `kind` — where this page came from: `session` (AI session), `doc` (imported
  document), `manual` (`memex remember`), `code` (repo analysis), `merged`
  (auto-consolidated near-duplicates). Informational only — no behavior.
- `status` — whether this page still holds: `current` (default), `superseded`
  (replaced — `superseded_by` link required), `obsolete` (project dead),
  `deprecated` (still useful, recommendation changed), `archived` (correct but
  dormant), `draft` (incomplete). Edit by hand or let the LLM propose.
- `## 📋 Histórico` — auto-maintained changelog at the bottom of each page
  (≤10 entries, one line per merge with a link to the raw source).
```

Na linha ~50 (`tier` no frontmatter), adicionar `kind` e `status`:

```python
- YAML frontmatter (`title`, `tags`, `kind`, `status`, `superseded_by`,
  `project`, `sources`, `updated`) is tool-owned — edit the body, leave
  the frontmatter to memex.
```

- [ ] **Step 2: Atualizar README.md**

Substituir a linha `- **Trust tiers:** ...` por:

```markdown
- **Page metadata:** `kind` (session/doc/manual/code/merged — where it came from) + `status` (current/superseded/obsolete/deprecated/archived/draft — whether it still holds). Auto-maintained `## 📋 Histórico` changelog on every page.
```

- [ ] **Step 3: Commit**

```bash
git add memex/vault.py README.md
git commit -m "docs: replace trust tiers with kind + status + changelog in schema and readme"
```


### Task 2: Adicionar `kind` ao raw note (ingest.py + capture.py)

**Files:**
- Modify: `memex/ingest.py:57-72,106-121,134,213,249-250,286,319`
- Modify: `memex/capture.py:69,76`

**Interfaces:**
- Consumes: (none — first code change)
- Produces: `_write_raw(..., kind=kind)` — novo parâmetro, substitui `tier`
- Produces: `ingest_session(vault, sess, seen, kind="session")` — renomeado
- Produces: `_ingest_docs` e `_ingest_index` usam `kind="doc"`
- Produces: `capture.py` passa `kind="session"` (não mais `tier_override`)

- [ ] **Step 1: Atualizar _write_raw**

```python
def _write_raw(vault, *, source, sid, date, cwd, kind, text):
    raw_dir = vault / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    datepart = (date or "")[:10] or "0000-00-00"
    uniq = hashlib.sha256(str(sid).encode()).hexdigest()[:8]
    fname = f"{datepart}--{source}--{_slugify(sid, 32)}--{uniq}.md"
    fm = (
        "---\n"
        f"source: {source}\n"
        f"id: {sid}\n"
        f"date: {date or ''}\n"
        f"cwd: {cwd or ''}\n"
        f"kind: {kind}\n"
        "---\n\n"
    )
    (raw_dir / fname).write_text(fm + scrub_mod.scrub(text or "").rstrip() + "\n", encoding="utf-8")
    return fname
```

- [ ] **Step 2: Atualizar ingest_session**

Mudar assinatura de `tier="silver"` para `kind="session"`:

```python
def ingest_session(vault, sess, seen, kind="session"):
    """Write ONE session dict (from memex/sources) into raw/, idempotently.
    Returns the raw filename, or None if unchanged/empty."""
    text = (sess or {}).get("text") or ""
    if not text.strip():
        return None
    key = f"{sess['source']}:{sess['id']}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    if key in seen:
        return None
    fname = _write_raw(
        vault, source=sess["source"], sid=sess["id"], date=sess.get("date"),
        cwd=sess.get("cwd"), kind=kind, text=text)
    _ledger_append(vault, key, fname)
    seen.add(key)
    return fname
```

- [ ] **Step 3: Atualizar _ingest_sessions**

Linha 134: trocar `tier = getattr(args, "tier_override", None) or "silver"` por `kind = "session"`:

```python
    workspace = getattr(args, "workspace", None)
    since = getattr(args, "since", None)
    n = 0
    print("ingesting sessions...")
    with ui.Progress("  scanning sessions") as bar:
        for sess in sources.iter_all(sources=src_names, workspace=workspace, since=since):
            fname = ingest_session(vault, sess, seen, kind="session")
```

- [ ] **Step 4: Atualizar _ingest_docs**

Linha 213: trocar `tier = getattr(args, "tier_override", None) or "silver"` por `kind = "doc"`. Linha 250: `tier=tier` → `kind=kind`:

```python
    kind = "doc"
    ...
    fname = _write_raw(vault, source="doc", sid=str(fp), date=_today(),
                       cwd=str(fp.parent), kind=kind, text=text)
```

- [ ] **Step 5: Atualizar _ingest_index**

Linha 286: trocar `tier = getattr(args, "tier_override", None) or "silver"` por `kind = "doc"`. Linha ~319: `tier=tier` → `kind=kind`:

```python
    kind = "doc"
    ...
    fname = _write_raw(vault, source="doc", sid=sid, date=_today(),
                       cwd=str(base or idx_dir), kind=kind, text=text)
```

- [ ] **Step 6: Atualizar capture.py**

Linhas 69 e 76: remover `tier_override=None` e adicionar `kind="session"` como kwarg explícito onde relevante. Como `ingest_session` já tem default `kind="session"`, só remover o `tier_override`:

Nas duas chamadas `ingest_mod.run(Namespace(...))`, remover `tier_override=None,`:

```python
# Linha 67-69:
        ingest_mod.run(Namespace(
            vault=str(vault), all=True, workspace=cwd, doc=None,
            docs=None, index=None, source="auto", since=None,
            session=None))

# Linha 73-76:
        ingest_mod.run(Namespace(
            vault=str(vault), all=False, workspace=None, doc=None,
            docs=cwd, index=None, source="auto", since=None,
            session=None, exclude=str(vault)))
```

- [ ] **Step 7: Commit**

```bash
git add memex/ingest.py memex/capture.py
git commit -m "feat: replace tier with kind in raw note frontmatter"
```


### Task 3: Atualizar synth.py — core (remover tier, adicionar kind + status + changelog)

**Files:**
- Modify: `memex/synth.py:8-10,29,64,200-203,324-339,477,577-579,590-597,606,612,622`
- Modify: `memex/synth.py` — PROPOSE_PROMPT e DISTILL_MERGE_PROMPT

**Interfaces:**
- Consumes: `kind` do raw note frontmatter (via `_read_frontmatter`)
- Consumes: `kind` e `status` existentes da wiki page (via `_read_frontmatter`)
- Produces: `_render_page(kind=kind, status=status, superseded_by=...)` substitui `tier=tier`
- Produces: `KIND_RANK` dict para precedência no merge
- Produces: bloco de changelog no `DISTILL_MERGE_PROMPT`

- [ ] **Step 1: Adicionar KIND_RANK e remover TIER_RANK**

```python
# Substituir linha 29:
KIND_RANK = {"merged": 0, "session": 1, "doc": 2, "code": 3, "manual": 4}

# Remover TIER_RANK (linha 29 antiga)
```

- [ ] **Step 2: Atualizar docstring**

Substituir linhas 8-10:

```python
"""memex synth — compile raw/ notes into the wiki/. The only LLM step.

Two-phase per raw note (provider-agnostic):
  1. propose (cheap model): where to file it (slug/section/tags/related) or skip.
  2. merge   (strong model): write/update the page, merging into existing content,
     with frontmatter + [[wikilinks]] + source citations + changelog.

Kinds (by source) are purely informational — no behavioral differences.
Pages carry a `status` field (current/superseded/obsolete/...) and an
auto-maintained `## 📋 Histórico` changelog section.
"""
```

- [ ] **Step 3: Atualizar PROPOSE_PROMPT — remover tier, adicionar kind**

Na linha ~64, trocar `tier={tier}` por `kind={kind}`:

```python
RAW NOTE (source={source}, kind={kind}):
{raw}
```

- [ ] **Step 4: Atualizar _index_summary — remover tier**

Linha 201, trocar `[{p.get('tier', 'silver')}]` por `[{p.get('kind', 'session')}]`:

```python
        f"- {p['slug']} [{p.get('kind', 'session')}] - {p.get('title', '')}: {p.get('summary', '')[:80]}"
```

- [ ] **Step 5: Atualizar _render_page — kind + status + superseded_by em vez de tier**

```python
def _render_page(*, title, tags, kind, status="current", superseded_by=None,
                 sources, body, project=None):
    """Build the page: memex-owned YAML frontmatter + the model's body."""
    def yaml_list(items):
        return ("\n" + "\n".join(f"  - {i}" for i in items)) if items else " []"

    safe_title = str(title).replace('"', "'")
    fm = (
        "---\n"
        f'title: "{safe_title}"\n'
        f"tags:{yaml_list(tags)}\n"
        f"kind: {kind}\n"
        f"status: {status}\n"
        + (f"superseded_by: {superseded_by}\n" if superseded_by else "")
        + (f"project: {project}\n" if project else "")
        + f"sources:{yaml_list(sources)}\n"
        f"updated: {date.today().isoformat()}\n"
        "---\n\n"
    )
    # kind label as opening blockquote
    kind_labels = {
        "session": "> 💬 Sessão de IA\n",
        "doc": "> 📄 Documento\n",
        "manual": "> ✍️ Salvo manualmente\n",
        "code": "> 🏛️ Código\n",
        "merged": "> 🔀 Consolidado\n",
    }
    label = kind_labels.get(kind, "")
    return fm + label + "\n" + (body or "").rstrip() + "\n"
```

- [ ] **Step 6: Atualizar DISTILL_MERGE_PROMPT — adicionar instrução de changelog**

Adicionar ao final do `DISTILL_MERGE_PROMPT` (antes do `EXISTING BODY`):

```python
- **Changelog (mandatory):** append exactly ONE line to the `## 📋 Histórico`
  section at the END of the page, summarizing what changed in this merge
  (1-2 sentences max). Format: `- \`YYYY-MM-DD\` — summary ([sessão](raw/date--source--id.md))`.
  Keep at most 10 entries (oldest first, newest last). If nothing substantive
  changed, skip. Never remove the section — if it doesn't exist, create it.
  Link to the raw source file: `raw/{raw_fname}`.
```

E adicionar `{raw_fname}` ao format do prompt:

```python
    merge_kwargs = dict(
        existing=existing_body_pre or "(none yet)", source=source, sid=sid,
        raw=raw_excerpt, raw_fname=f.name,
        related=", ".join(f"[[{r}]]" for r in related) or "(none)")
```

- [ ] **Step 7: Atualizar phase 3 (write) — usar kind + status em vez de tier**

Substituir linhas 577-597:

```python
            # Resolve kind: the raw note's kind, or if existing page has a
            # stronger kind, keep that (manual > code > doc > session > merged)
            raw_kind = meta.get("kind", "session")
            if raw_kind not in KIND_RANK:
                raw_kind = "session"
            new_kind = raw_kind
            if existing and KIND_RANK.get(existing.get("kind", "session"), 1) >= KIND_RANK.get(raw_kind, 1):
                new_kind = existing.get("kind", "session")

            # Preserve existing status and superseded_by unless the raw note
            # explicitly signals a change
            new_status = existing.get("status", "current") if existing else "current"
            new_superseded_by = existing.get("superseded_by") if existing else None

            # Re-filter related slugs against the now-current pages_by_slug
            related_now = [r for r in related if r in pages_by_slug]
            merged_body = _prune_wikilinks(merged_body, set(pages_by_slug) | set(related_now))
            merged_body = _dedup_blocks(merged_body)

            src_ref = f"{source}:{sid}"
            sources = list(dict.fromkeys((existing.get("sources", []) if existing else []) + [src_ref]))
            tags = _clean_tags((existing.get("tags", []) if existing else []) + (prop.get("tags") or []), max_tags=lim["max_tags"])
            title = (existing.get("title") if existing else None) or prop.get("title") or slug
            page_text = _render_page(title=title, tags=tags, kind=new_kind,
                                     status=new_status, superseded_by=new_superseded_by,
                                     sources=sources, body=merged_body, project=project)
```

- [ ] **Step 8: Atualizar index entry — kind em vez de tier**

Linha ~606:

```python
            pages_by_slug[slug] = {
                "slug": slug, "title": title,
                "section": (existing.get("section", section) if existing else section),
                "kind": new_kind, "status": new_status,
                "tags": tags, "sources": sources, "project": project,
                "summary": _summary_from(prop.get("distill") or (existing.get("summary") if existing else "") or ""),
                "path": rel,
            }
```

- [ ] **Step 9: Atualizar changelog JSONL — kind e status em vez de tier**

Linha ~612:

```python
            with changelog.open("a", encoding="utf-8") as ch:
                ch.write(json.dumps({
                    "ts": int(time.time()), "page": slug, "kind": new_kind,
                    "status": new_status,
                    "action": "update" if existing_full else "create",
                    "source": f"{source}:{sid}", "raw": f.name}) + "\n")
```

- [ ] **Step 10: Commit**

```bash
git add memex/synth.py
git commit -m "feat: replace tier with kind + status in synth (pages, prompts, index)"
```


### Task 4: Atualizar analyze.py (kind=code)

**Files:**
- Modify: `memex/analyze.py:11,231-251`

**Interfaces:**
- Consumes: `synth._render_page(kind=..., status=...)` e `synth.KIND_RANK` (se precisar)
- Produces: páginas de arquitetura com `kind: code, status: current`

- [ ] **Step 1: Atualizar analyze.py**

Remover snapshot de gold (linhas 231-235), trocar `tier="gold"` por `kind="code"`:

```python
    for slug, title, body in pages:
        page_path = vault / "wiki" / "topics" / f"{slug}.md"
        existed = page_path.exists()
        page_path.parent.mkdir(parents=True, exist_ok=True)
        tags = ["architecture", repo_tag]
        page_path.write_text(synth._render_page(
            title=title, tags=tags, kind="code", status="current",
            sources=[src], body=body,
            project=repo_tag), encoding="utf-8")
        by_slug[slug] = {
            "slug": slug, "title": title, "section": "topics", "kind": "code",
            "status": "current",
            "tags": tags, "sources": [src], "project": repo_tag,
            "summary": _extract_summary(body or ""),
            "path": str(page_path.relative_to(vault / "wiki")),
        }
        with changelog.open("a", encoding="utf-8") as ch:
            ch.write(json.dumps({
                "ts": int(time.time()), "page": slug, "kind": "code",
                "status": "current",
                "action": "update" if existed else "create",
                "source": src, "raw": "analyze"}) + "\n")
```

- [ ] **Step 2: Atualizar docstring (linha 11)**

```python
# Pages are code kind, written straight to wiki/.
```

- [ ] **Step 3: Commit**

```bash
git add memex/analyze.py
git commit -m "feat: replace tier=gold with kind=code in analyze"
```


### Task 5: Atualizar gardening.py (kind=merged)

**Files:**
- Modify: `memex/gardening.py:235,255-262`

**Interfaces:**
- Consumes: `synth._render_page(kind="merged", status=...)` e `synth.KIND_RANK` (removido)
- Produces: páginas consolidadas com `kind: merged`

- [ ] **Step 1: Atualizar gardening.py — remover TIER_RANK, usar kind="merged"**

```python
        canon = min(g, key=lambda m: len(m.get("slug", "")))
        # kind = "merged" (consolidated from near-duplicates)
        sources = list(dict.fromkeys(s for m in g for s in (m.get("sources") or [])))
        tags = list(dict.fromkeys(t for m in g for t in (m.get("tags") or [])))[:8]
        title = canon.get("title") or canon["slug"]
```

Linha ~254: `tier=tier` → `kind="merged"` + `status="current"`:

```python
        (vault / "wiki" / canon["path"]).write_text(
            synth._render_page(title=title, tags=tags, kind="merged", status="current",
                               sources=sources, body=merged,
                               project=canon.get("project")), encoding="utf-8")
```

Linha ~258: `"tier": tier` → `"kind": "merged", "status": "current"`:

```python
        canon.update({"title": title, "kind": "merged", "status": "current",
                      "tags": tags, "sources": sources})
```

Linha ~261: `"tier": tier` → `"kind": "merged"`:

```python
            ch.write(json.dumps({
                "ts": int(time.time()), "page": canon["slug"], "kind": "merged",
                "status": "current",
                "action": "garden-merge",
                "absorbed": [m["slug"] for m in g if m["slug"] != canon["slug"]],
            }) + "\n")
```

- [ ] **Step 2: Commit**

```bash
git add memex/gardening.py
git commit -m "feat: replace tier with kind=merged in gardening"
```


### Task 6: Atualizar CLI — remover --tier, mostrar kinds

**Files:**
- Modify: `memex/cli.py:94-102,284`

**Interfaces:**
- Consumes: index entries com `kind` e `status` em vez de `tier`
- Produces: `memex status` mostra `kinds` em vez de `tiers`

- [ ] **Step 1: Atualizar status display (linhas 94-102)**

```python
    kinds: dict[str, int] = {}
    for p in pages:
        k = p.get("kind", "session")
        kinds[k] = kinds.get(k, 0) + 1
    statuses: dict[str, int] = {}
    for p in pages:
        s = p.get("status", "current")
        statuses[s] = statuses.get(s, 0) + 1
    workspace_pages = sorted((vault_dir / "workspace").glob("*.md")) if (vault_dir / "workspace").is_dir() else []
    print(f"vault: {vault_dir}")
    print(f"  raw notes  : {len(raw)}")
    print(f"  synthesized: {len(synthed)}  (pending: {max(0, len(raw) - len(synthed))})")
    print(f"  wiki pages : {len(pages)}  kinds={dict(kinds)}  statuses={dict(statuses)}")
```

- [ ] **Step 2: Remover --tier da CLI (linha 284)**

Remover a linha:
```python
    pg.add_argument("--tier", dest="tier_override", choices=["gold", "silver", "bronze"])
```

- [ ] **Step 3: Commit**

```bash
git add memex/cli.py
git commit -m "feat: show kinds/statuses in CLI, remove --tier flag"
```


### Task 7: Atualizar MCP server — status output

**Files:**
- Modify: `memex/mcp_server.py:9,88-91,212-215,232-233`

**Interfaces:**
- Consumes: index entries com `kind` e `status`
- Produces: `status` tool retorna `kinds` e `statuses` em vez de `tiers`

- [ ] **Step 1: Atualizar docstring (linha 9)**

```python
  status   — peek at the brain: raw notes, wiki pages, pending, workspace-pages
```

- [ ] **Step 2: Atualizar descrição da tool status (linhas 88-91)**

```python
        "description": "Peek at the memex brain — how many raw notes, wiki pages, "
                       "pending synthesis, workspace-pages, kinds, and statuses. "
                       "Use when the user asks about their brain's state.",
```

- [ ] **Step 3: Atualizar _tool_status (linhas 212-233)**

```python
    kinds = {}
    statuses = {}
    for p in pages:
        k = p.get("kind", "session")
        kinds[k] = kinds.get(k, 0) + 1
        s = p.get("status", "current")
        statuses[s] = statuses.get(s, 0) + 1

    workspace_pages = sorted((vault / "workspace").glob("*.md")) if (vault / "workspace").is_dir() else []

    ...

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
```

- [ ] **Step 4: Commit**

```bash
git add memex/mcp_server.py
git commit -m "feat: show kinds and statuses in MCP status tool"
```


### Task 8: Atualizar testes

**Files:**
- Modify: `tests/test_memex.py` — múltiplas linhas com `tier`, `write_now`, `read_now`, etc.
- Modify: `tests/mock_llm.py:22-26`

**Interfaces:**
- Consumes: `kind`, `status`, `workspace_mod` (já renomeado de `now_mod`)
- Produces: 52+ testes passando com asserts de `kind`/`status` em vez de `tier`

- [ ] **Step 1: Atualizar mock_llm.py**

```python
        elif "WORKING-MEMORY" in prompt:                # workspace-page
            content = ("## Contexto\nPipeline de vendas: dedup de pedidos duplicados.\n\n"
                       "## Estado atual\nRegra order_id + janela 24h definida e validada.\n\n"
                       "## Próximos passos\n- [ ] aplicar a regra no job noturno\n\n"
                       "## Arquivos-chave\n- etl/dedup.sql — a regra\n")
```

- [ ] **Step 2: Atualizar test_memex.py — busca e substituição global**

```bash
# Rodar substituições:
grep -n "tier" tests/test_memex.py  # listar todas as ocorrências
```

Substituições manuais:

a) Linha ~63: `# now-page generation` → `# workspace-page generation`
b) Linhas com `.get("tier"` → `.get("kind"`
c) Linhas com `"tier":` → `"kind":` + adicionar `"status": "current"`
d) Linhas com `tier=` → `kind=`
e) Linhas com `tiers` (dict) → `kinds`
f) Linhas com `_write_raw(..., tier=tier,` → `_write_raw(..., kind=kind,`
g) Linha ~788: teste que verifica `--tier` removido — atualizar para verificar que `--tier` é rejeitado

- [ ] **Step 3: Rodar testes**

```bash
python -m pytest tests/test_memex.py -v
```

Esperado: 52 passando (ou mais, se adicionarmos testes novos).

- [ ] **Step 4: Commit**

```bash
git add tests/test_memex.py tests/mock_llm.py
git commit -m "test: update tests for kind/status replacing tier"
```


### Task 9: Script de migração do vault real

**Files:**
- Create: `memex/migrate_kind.py`

**Interfaces:**
- Consumes: `vault / ".memex" / "index.json"` com `tier`, `sources`
- Produces: `index.json` atualizado com `kind`, `status`; wiki pages com frontmatter atualizado; raw notes com `kind`

- [ ] **Step 1: Criar script de migração**

```python
"""One-shot migration: replace `tier` with `kind` + `status` in existing vault."""

import json
import sys
from pathlib import Path


def migrate(vault_path):
    vault = Path(vault_path).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault")
        return 1

    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        print("no index found — nothing to migrate")
        return 0

    pages = idx.get("pages", [])
    updated = 0

    for p in pages:
        # Infer kind from sources
        raw_sources = p.get("sources", [])
        kind = "session"  # default
        for src in raw_sources:
            src_str = str(src)
            if src_str.startswith("remember:"):
                kind = "manual"
                break
            elif src_str.startswith("analyze:"):
                kind = "code"
                break
            elif src_str.startswith("doc:"):
                kind = "doc"
                break

        # Add kind and status, remove tier
        p["kind"] = kind
        p["status"] = "current"
        p.pop("tier", None)

        # Update the wiki page file
        page_path = vault / "wiki" / p.get("path", "")
        if page_path.exists():
            text = page_path.read_text(encoding="utf-8")
            # Replace tier in frontmatter
            lines = text.splitlines()
            new_lines = []
            in_fm = False
            fm_done = False
            for i, line in enumerate(lines):
                if i == 0 and line.strip() == "---":
                    in_fm = True
                    new_lines.append(line)
                    continue
                if in_fm and line.strip() == "---":
                    # Add kind + status before closing ---
                    new_lines.append(f"kind: {kind}")
                    new_lines.append("status: current")
                    new_lines.append(line)
                    in_fm = False
                    fm_done = True
                    continue
                if in_fm and line.startswith("tier:"):
                    continue  # skip tier line
                new_lines.append(line)

            if fm_done:
                page_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        updated += 1

    idx["pages"] = pages
    idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")

    # Update raw notes: tier: → kind:
    raw_dir = vault / "raw"
    raw_updated = 0
    for raw_file in raw_dir.glob("*.md"):
        text = raw_file.read_text(encoding="utf-8")
        if "tier:" in text:
            text = text.replace("tier:", "kind:")
            raw_file.write_text(text, encoding="utf-8")
            raw_updated += 1

    # Update synthed.json keys (tier not stored there, nothing to do)

    print(f"✓ migrated {updated} wiki pages")
    print(f"✓ migrated {raw_updated} raw notes")
    return 0


if __name__ == "__main__":
    vault = sys.argv[1] if len(sys.argv) > 1 else None
    if not vault:
        print("usage: python -m memex.migrate_kind <vault-path>")
        sys.exit(1)
    sys.exit(migrate(vault))
```

- [ ] **Step 2: Rodar migração**

```bash
python -m memex.migrate_kind /Users/gian.moraes/memex
```

- [ ] **Step 3: Verificar**

```bash
# Checar que não tem mais "tier" no index
python -c "import json; idx=json.load(open('/Users/gian.moraes/memex/.memex/index.json')); print({p.get('kind') for p in idx['pages']}); print({p.get('status') for p in idx['pages']})"

# Checar um raw note
head -10 /Users/gian.moraes/memex/raw/*.md | grep -E "^(tier|kind):" | head -5
```

- [ ] **Step 4: Commit**

```bash
git add memex/migrate_kind.py
git commit -m "feat: add one-shot migration script for kind/status"
```


### Task 10: Verificação final

- [ ] **Step 1: Rodar suite completa de testes**

```bash
python -m pytest tests/test_memex.py -v
```
Esperado: 52+ passando, 0 failures.

- [ ] **Step 2: Verificar MCP status**

```bash
python -c "
from memex.mcp_server import _tool_status
import json
result = _tool_status('/Users/gian.moraes/memex')
print(json.dumps(result, indent=2, ensure_ascii=False))
"
```
Esperado: `kinds` e `statuses` no output, sem `tiers`.

- [ ] **Step 3: Verificar CLI status**

```bash
python -m memex.cli status --vault /Users/gian.moraes/memex
```
Esperado: `kinds=` e `statuses=` no output, sem `tiers`.

- [ ] **Step 4: Verificar wiki page migrada**

```bash
head -15 /Users/gian.moraes/memex/wiki/topics/*.md | grep -E "^(tier|kind|status):" | head -5
```
Esperado: `kind:` e `status:` presentes, sem `tier:`.

- [ ] **Step 5: Commit final**

```bash
git add -A
git commit -m "chore: final verification after kind/status migration"
```
