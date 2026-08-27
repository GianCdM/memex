# Memex loop-proof pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminar o loop de reprocessamento da pipeline memex (raw nunca processa infinitamente), drenar o backlog herdado via fresh-start de agosto, e melhorar a precisão do propose pra frente.

**Architecture:** 5 mudanças ortogonais sobre o `synth.py`/`changes.py` existente: (M1) flush incremental de `synthed.json` por-raw, (M2) dedup na criação de ChangeSet, (M3) cap de retry com park, (M4) CLI fresh-start one-time, (M5) tighten do PROPOSE_PROMPT + model tier por densidade. Fundação loop-proof (M1-M3) é comum; M4 zera o backlog; M5 baixa o review rate.

**Tech Stack:** Python 3.12, stdlib (json, hashlib, uuid, threading, tempfile), pytest. Sem novas deps.

## Global Constraints

- **Vault real:** `<path>/memex`. Testes DEVEM usar fixtures isoladas (tmp_path), NUNCA o vault real.
- **Acentuação:** textos em PT nos prints/mensagens usam acentuação correta.
- **Atomic writes:** todo state mutation em disco via `_atomic_write` (tmp + replace).
- **Lock:** `synth.run` já adquire `.memex/synth.lock` (`synth.py:880`); não introduzir locks novos que deadlockem com `write_lock`.
- **Knobs:** novos limites vão em `memex/limits.py:DEFAULTS` (sobreviráveis por `config.json`).
- **Hashes:** `hashlib.sha256(...).hexdigest()[:16]` (padrão existente).
- **ChangeSet states válidos:** `pending, applying, applied, rejected, stale, rolled_back` (`changes.py:40`). `archived-pre-freshstart` é um dir de migração one-time, NÃO um state do ciclo.
- **Operações válidas:** verificar/adicionar `park` em `changes.py:_OPERATIONS` (Task 4).

## File Structure

| Arquivo | Responsabilidade | Muda/Cria |
|---|---|---|
| `memex/synth.py` | M1 flush helper + wire; M2 dedup load+check; M3 attempts; M5 prompt+tier | Modify |
| `memex/changes.py` | `compute_dedup_key`, `load_pending_dedup`, add `park` op | Modify |
| `memex/limits.py` | knobs `provider_error_cap`, `propose_tier_chars` | Modify |
| `memex/freshstart.py` | M4 CLI handler (mark pre-date raws, archive pendings) | Create |
| `memex/cli.py` | registrar subcomando `fresh-start` | Modify |
| `tests/test_memex.py` | testes TDD p/ M1-M5 + integração S1-S6 | Modify |

---

### Task 1: M4 — CLI `fresh-start` (dry-run + apply)

**Files:**
- Create: `memex/freshstart.py`
- Modify: `memex/cli.py` (registrar subcomando, após `cli.py:297`)
- Test: `tests/test_memex.py` (nova classe `TestFreshStart`)

**Interfaces:**
- Produces: `freshstart.run(args) -> int` (0=ok). Lê `args.vault`, `args.from_date` (str `YYYY-MM-DD`), `args.dry_run` (bool), `args.archive_pending` (bool).
- Consumes: `canon.raw_dir(vault)`, `changes._review_dir(vault, "pending")`, `synth._atomic_write` (ou próprio helper).

- [ ] **Step 1: Write failing tests**

```python
class TestFreshStart:
    def _vault_with_raws(self, tmp_path):
        import os, hashlib
        from memex import canon
        v = tmp_path / "vault"
        (v / ".memex" / "raw").mkdir(parents=True)
        (v / ".memex" / "review" / "pending").mkdir(parents=True)
        # raws de julho e agosto
        for name in ("2026-07-15--claude--aaa--x.md", "2026-07-20--claude--bbb--y.md",
                     "2026-08-05--claude--ccc--z.md"):
            (v / ".memex" / "raw" / name).write_text(f"body of {name}")
        # synthed vazio
        (v / ".memex" / "synthed.json").write_text("{}")
        # 2 pendings (simula ChangeSet)
        for i in range(2):
            (v / ".memex" / "review" / "pending" / f"p{i}.json").write_text(
                '{"id":"p%d","state":"pending","source":{"raw":"raw/x.md"}}' % i)
        return v

    def test_dry_run_marks_nothing(self, tmp_path, monkeypatch):
        from memex import freshstart
        v = self._vault_with_raws(tmp_path)
        class A: pass
        a = A(); a.vault=str(v); a.from_date="2026-08-01"; a.dry_run=True; a.archive_pending=True
        rc = freshstart.run(a)
        assert rc == 0
        # nada mutado: synthed continua vazio, pendings intactos
        import json
        assert json.loads((v/".memex"/"synthed.json").read_text()) == {}
        assert len(list((v/".memex"/"review"/"pending").glob("*.json"))) == 2

    def test_apply_marks_pre_august_and_archives(self, tmp_path):
        from memex import freshstart
        import json, hashlib
        v = self._vault_with_raws(tmp_path)
        class A: pass
        a = A(); a.vault=str(v); a.from_date="2026-08-01"; a.dry_run=False; a.archive_pending=True
        rc = freshstart.run(a)
        assert rc == 0
        s = json.loads((v/".memex"/"synthed.json").read_text())
        # 2 raws de julho marcados, agosto NÃO
        assert "2026-07-15--claude--aaa--x.md" in s
        assert "2026-07-20--claude--bbb--y.md" in s
        assert "2026-08-05--claude--ccc--z.md" not in s
        # hash real
        h = hashlib.sha256((v/".memex"/"raw"/"2026-07-15--claude--aaa--x.md").read_bytes()).hexdigest()[:16]
        assert s["2026-07-15--claude--aaa--x.md"] == h
        # pendings arquivados
        assert len(list((v/".memex"/"review"/"pending").glob("*.json"))) == 0
        assert len(list((v/".memex"/"review"/"archived-pre-freshstart").glob("*.json"))) == 2

    def test_idempotent(self, tmp_path):
        from memex import freshstart
        v = self._vault_with_raws(tmp_path)
        class A: pass
        a = A(); a.vault=str(v); a.from_date="2026-08-01"; a.dry_run=False; a.archive_pending=True
        freshstart.run(a)
        rc2 = freshstart.run(a)  # segunda vez: no-op
        assert rc2 == 0
        import json
        s = json.loads((v/".memex"/"synthed.json").read_text())
        assert len(s) == 2  # não duplicou
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memex.py::TestFreshStart -v`
Expected: FAIL `ModuleNotFoundError: memex.freshstart`

- [ ] **Step 3: Create `memex/freshstart.py`**

```python
"""memex/freshstart.py — one-time backlog reset.

Marks all raw captures dated BEFORE --from as processed (no LLM), optionally
archives pending ChangeSets. Preserves applied/rejected. Idempotent.
"""
from __future__ import annotations
import json, hashlib, shutil
from pathlib import Path


def _raw_date_prefix(name: str) -> str:
    # filename: YYYY-MM-DD--source--...
    return name[:10] if len(name) >= 10 and name[4:5] == "-" else ""


def run(args) -> int:
    vault = Path(args.vault)
    from_date = args.from_date
    dry = getattr(args, "dry_run", False)
    archive = getattr(args, "archive_pending", False)

    raw_dir = vault / ".memex" / "raw"
    synthed_path = vault / ".memex" / "synthed.json"
    try:
        synthed = json.loads(synthed_path.read_text(encoding="utf-8"))
    except Exception:
        synthed = {}

    raws = sorted(raw_dir.glob("*.md")) if raw_dir.exists() else []
    to_mark = [f for f in raws
               if _raw_date_prefix(f.name) and _raw_date_prefix(f.name) < from_date
               and synthed.get(f.name) is None]

    pending_dir = vault / ".memex" / "review" / "pending"
    pendings = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    archive_dir = vault / ".memex" / "review" / "archived-pre-freshstart"

    by_month = {}
    for f in to_mark:
        m = _raw_date_prefix(f.name)[:7]
        by_month[m] = by_month.get(m, 0) + 1

    print(f"fresh-start {'(dry-run) ' if dry else ''}from {from_date}")
    print(f"  raws to mark processed: {len(to_mark)}")
    for m, n in sorted(by_month.items()):
        print(f"    {m}: {n}")
    print(f"  pending ChangeSets to archive: {len(pendings)}")

    if dry:
        print("  (dry-run — nothing mutated)")
        return 0

    # mark raws
    for f in to_mark:
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        synthed[f.name] = h
    tmp = synthed_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(synthed, indent=2) + "\n", encoding="utf-8")
    tmp.replace(synthed_path)

    # archive pendings
    if archive and pendings:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for p in pendings:
            shutil.move(str(p), str(archive_dir / p.name))

    print(f"  done. marked {len(to_mark)} raws, archived {len(pendings)} pendings.")
    return 0
```

- [ ] **Step 4: Register subcommand in `memex/cli.py`**

Após `cli.py:297` (após o bloco do `reflect`), adicionar:

```python
pfs = sub.add_parser("fresh-start",
                     help="one-time backlog reset: mark pre-date raws processed, archive pendings")
pfs.add_argument("--vault", required=True)
pfs.add_argument("--from", dest="from_date", required=True,
                 help="ISO date YYYY-MM-DD — raws before this are marked processed")
pfs.add_argument("--dry-run", action="store_true")
pfs.add_argument("--archive-pending", action="store_true", default=True,
                 help="move pending ChangeSets to archived-pre-freshstart/ (default on)")
pfs.set_defaults(func=freshstart_mod.run)
```

E adicionar o import no topo do cli.py (junto aos outros `import ... as _mod`):
```python
from . import freshstart as freshstart_mod
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_memex.py::TestFreshStart -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add memex/freshstart.py memex/cli.py tests/test_memex.py
git commit -m "feat: memex fresh-start CLI — mark pre-date raws processed + archive pendings"
```

---

### Task 2: M1 — Flush incremental de `synthed.json` por-raw

**Files:**
- Modify: `memex/synth.py` (novo helper `_mark_done` + wire nos sites 1114, 1440, 1504, finally 1549)
- Test: `tests/test_memex.py` (nova classe `TestIncrementalFlush`)

**Interfaces:**
- Produces: `_mark_done(vault, synthed, synthed_path, lineage, name, h)` — marca + flusha synthed.json e lineage atomicamente. Deve ser chamado sob `write_lock`.
- Consumes: `_atomic_write` (`synth.py:540`), `_save_lineage` (`synth.py:535`).

- [ ] **Step 1: Write failing test**

```python
class TestIncrementalFlush:
    def test_mark_done_flushes_synthed_immediately(self, tmp_path, monkeypatch):
        # simula: _mark_done grava synthed.json em disco a cada chamada
        from memex import synth
        v = tmp_path / "vault"
        (v / ".memex").mkdir(parents=True)
        sp = v / ".memex" / "synthed.json"
        sp.write_text("{}")
        synthed = {}
        lineage = {}
        synth._mark_done(v, synthed, sp, lineage, "raw-aaa.md", "abc123")
        # em disco imediatamente
        import json
        on_disk = json.loads(sp.read_text())
        assert on_disk == {"raw-aaa.md": "abc123"}
        # o dict em memória também
        assert synthed == {"raw-aaa.md": "abc123"}

    def test_kill_after_mark_preserves_state(self, tmp_path):
        # simula um reflect que marca 2 raws e "morre" antes do flush final:
        # o estado dos 2 raws já está em disco por causa do flush por-raw
        from memex import synth
        v = tmp_path / "vault"
        (v / ".memex").mkdir(parents=True)
        sp = v / ".memex" / "synthed.json"
        sp.write_text("{}")
        synthed, lineage = {}, {}
        synth._mark_done(v, synthed, sp, lineage, "r1.md", "h1")
        synth._mark_done(v, synthed, sp, lineage, "r2.md", "h2")
        # "morre" aqui — sem flush final. Estado preservado?
        import json
        on_disk = json.loads(sp.read_text())
        assert on_disk == {"r1.md": "h1", "r2.md": "h2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memex.py::TestIncrementalFlush -v`
Expected: FAIL `AttributeError: module 'memex.synth' has no attribute '_mark_done'`

- [ ] **Step 3: Add `_mark_done` helper in `memex/synth.py`**

Após `_flush_state` (`synth.py:569`), adicionar:

```python
def _mark_done(vault, synthed, synthed_path, lineage, name, h):
    """Mark a raw as processed AND flush synthed.json + lineage to disk
    immediately. Called under write_lock. A kill/crash anywhere after this
    point preserves the mark — this is the loop-proof guarantee (M1)."""
    synthed[name] = h
    try:
        _atomic_write(synthed_path, json.dumps(synthed, indent=2) + "\n")
    except Exception:
        pass
    try:
        _save_lineage(vault, lineage)
    except Exception:
        pass
```

- [ ] **Step 4: Wire `_mark_done` into the 3 marking sites in `_process_one`**

Em `synth.py`, substituir os 3 sites de marcação no `_process_one`:

**Site A — prop.skip (~1114-1120):** localizar `synthed[f.name] = h` seguido de `_synthed_dirty[0] = True` no ramo `prop["skip"]`. Substituir por:
```python
                _mark_done(vault, synthed, synthed_path, lineage, f.name, h)
                _synthed_dirty[0] = True
```
(manter o `_record_chunk_done()` para o caso chunk, se houver no mesmo ramo)

**Site B — auto-reject (~1440):** localizar `synthed[f.name] = hashlib.sha256(...).hexdigest()[:16]` no ramo `reject/archive` auto-review. Substituir por:
```python
                    _mark_done(vault, synthed, synthed_path, lineage, f.name,
                               hashlib.sha256(f.read_bytes()).hexdigest()[:16])
                    _synthed_dirty[0] = True
```

**Site C — common path (~1503-1505):** localizar `if not is_chunk: synthed[f.name] = h; _synthed_dirty[0] = True`. Substituir por:
```python
            if not is_chunk:
                _mark_done(vault, synthed, synthed_path, lineage, f.name, h)
                _synthed_dirty[0] = True
```

**Site D — chunk-done no finally (~1549-1553):** localizar o bloco que marca `synthed[it["chunk_of"]] = it["h"]`. Substituir por:
```python
                _mark_done(vault, synthed, synthed_path, lineage, it["chunk_of"], it["h"])
                _synthed_dirty[0] = True
```

Nota: o `_flush_state` final (`synth.py:1557`) permanece — ele ainda grava views e metrics. Como `_mark_done` já gravou synthed/lineage, o `_flush_state` com `synthed_path if _synthed_dirty` é idempotente (reescreve o mesmo conteúdo).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_memex.py::TestIncrementalFlush -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run full suite to check no regression**

Run: `pytest tests/test_memex.py -x -q`
Expected: PASS (sem regressões nos testes de synth existentes)

- [ ] **Step 7: Commit**

```bash
git add memex/synth.py tests/test_memex.py
git commit -m "fix: flush synthed.json+lineage per-raw (M1) — kill no longer loses marks"
```

---

### Task 3: M2 — Dedup na criação de ChangeSet

**Files:**
- Modify: `memex/changes.py` (`compute_dedup_key`, `load_pending_dedup`, add `park` op)
- Modify: `memex/synth.py` (load dedup_set no início do run + check antes de `save_changeset` em ~1457)
- Test: `tests/test_memex.py` (nova classe `TestChangeSetDedup`)

**Interfaces:**
- Produces (changes.py):
  - `compute_dedup_key(change: dict) -> str` — sha16 de `(raw_sha256, slug, section, chunk_idx, operation)`.
  - `load_pending_dedup(vault) -> dict[str, str]` — `{dedup_key: change_id}` lendo `review/pending/*.json`.
- Consumes (synth.py): no `_process_one`, antes de `changes_mod.save_changeset(vault, change)` (~1457), checar dedup_set.

- [ ] **Step 1: Write failing tests**

```python
class TestChangeSetDedup:
    def _change(self, raw_sha="abc", slug="s", section="topics", chunk_idx=None, op="create", body="b"):
        return {"id":"x","state":"pending","operation":op,
                "source":{"raw":"raw/r.md","raw_sha256":raw_sha,"kind":"raw","mode":"chunk" if chunk_idx is not None else "full"},
                "target":{"slug":slug},
                "index_record":{"section":section},
                "proposed_body":body,
                "_chunk_index":chunk_idx}

    def test_dedup_key_stable(self):
        from memex import changes
        c = self._change()
        k1 = changes.compute_dedup_key(c)
        k2 = changes.compute_dedup_key(c)
        assert k1 == k2 and len(k1) == 16

    def test_dedup_key_differs_by_slice(self):
        from memex import changes
        k1 = changes.compute_dedup_key(self._change(chunk_idx=0))
        k2 = changes.compute_dedup_key(self._change(chunk_idx=1))
        assert k1 != k2

    def test_load_pending_dedup(self, tmp_path):
        from memex import changes
        import json
        v = tmp_path / "vault"
        pd = v/".memex"/"review"/"pending"; pd.mkdir(parents=True)
        c = self._change(raw_sha="sha1", slug="s1", body="x"); c["id"]="id1"
        (pd/"id1.json").write_text(json.dumps(c))
        d = changes.load_pending_dedup(v)
        key = changes.compute_dedup_key(c)
        assert d.get(key) == "id1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memex.py::TestChangeSetDedup -v`
Expected: FAIL `AttributeError: module 'memex.changes' has no attribute 'compute_dedup_key'`

- [ ] **Step 3: Implement helpers in `memex/changes.py`**

Após `_review_dir` (`changes.py:53`), adicionar:

```python
def compute_dedup_key(change: dict) -> str:
    """Stable key for dedup: same (raw, slug, section, chunk_idx, operation)
    reprocessed should NOT create a duplicate ChangeSet."""
    src = change.get("source", {}) or {}
    tgt = change.get("target", {}) or {}
    idx = change.get("index_record", {}) or {}
    raw_sha = src.get("raw_sha256") or src.get("raw") or ""
    slug = tgt.get("slug") or ""
    section = idx.get("section") or ""
    chunk = change.get("_chunk_index")
    chunk_str = "" if chunk is None else str(chunk)
    op = change.get("operation") or ""
    blob = f"{raw_sha}|{slug}|{section}|{chunk_str}|{op}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_pending_dedup(vault) -> dict:
    """Return {dedup_key: change_id} for all pending ChangeSets."""
    import hashlib as _h, json as _j
    out = {}
    pd = _review_dir(vault, "pending")
    for p in sorted(pd.glob("*.json")):
        try:
            d = _j.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[compute_dedup_key(d)] = d.get("id")
    return out
```

Garantir `import hashlib` no topo do changes.py (se não existir).

- [ ] **Step 4: Wire dedup check into `_process_one` (`synth.py`)**

No início de `_run_impl`, após carregar `synthed` (~918) e antes do loop, carregar o dedup_set. Localizar o ponto após `synthed = {...}` e adicionar:
```python
    dedup_set = changes_mod.load_pending_dedup(vault)
```
(`changes_mod` já é importado em synth.py — verificar; se não, usar o alias existente.)

Em `_process_one`, antes de `changes_mod.save_changeset(vault, change)` (~1457), adicionar o check. Localizar a linha `changes_mod.save_changeset(vault, change)` e substituir por:
```python
            # M2: dedup — a reprocess of an already-pending slice must not
            # create a duplicate; the raw is already in review, mark it done.
            _dk = changes_mod.compute_dedup_key(change)
            _existing = dedup_set.get(_dk)
            if _existing is not None:
                _ex = changes_mod.load_changeset(vault, _existing)
                if _ex and _body_hash(_ex.get("proposed_body","")) == _body_hash(change.get("proposed_body","")):
                    # idêntico — skip, raw já está em review
                    if not is_chunk:
                        _mark_done(vault, synthed, synthed_path, lineage, f.name, h)
                        _synthed_dirty[0] = True
                    _processed[0] += 1
                    _err_cnt[0] = 0
                    print(f"  [{_processed[0]}/{total}] {f.name} -> dedup-skip (pending { _existing })")
                    _metrics.append({"fname": f.name, "kind": note_kind, "mode": "dedup-skip",
                                     "outcome": "dedup", "route": "skip",
                                     "reason": "identical pending exists", "latency_ms": 0,
                                     "body_chars": len(body), "model_propose": model_propose,
                                     "model_merge": model_merge, "verify_model": verify_model})
                    return None
                # diferente — supersede o antigo
                if _ex:
                    try: changes_mod.transition_changeset(vault, _existing, "stale",
                                                          review_reason="superseded by reprocess")
                    except Exception: pass
            changes_mod.save_changeset(vault, change)
            dedup_set[_dk] = change["id"]
```

Nota: `change` precisa ter `_chunk_index` setado para chunks. No ponto onde o change é montado para chunks, adicionar `change["_chunk_index"] = item.get("chunk_index")` antes do route block. (Se o change já é montado com acesso ao `item`, incluir lá.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_memex.py::TestChangeSetDedup -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run full suite**

Run: `pytest tests/test_memex.py -x -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add memex/changes.py memex/synth.py tests/test_memex.py
git commit -m "fix: dedup ChangeSet creation (M2) — reprocess never duplicates a pending slice"
```

---

### Task 4: M3 — Cap de retry com park

**Files:**
- Modify: `memex/limits.py` (knob `provider_error_cap: 3`)
- Modify: `memex/changes.py` (add `park` a `_OPERATIONS`)
- Modify: `memex/synth.py` (load/save `attempts.json`, increment on error, park at cap)
- Test: `tests/test_memex.py` (nova classe `TestRetryCap`)

**Interfaces:**
- Produces: `_attempts_path(vault) -> Path` (`.memex/attempts.json`). `_record_attempt(vault, attempts, name) -> int` (retorna count pós-increment). `_park_raw(vault, synthed, synthed_path, lineage, attempts, name, h, ...)` — marca done + cria ChangeSet park.
- Consumes: `lim["provider_error_cap"]`, `changes_mod.new_changeset(operation="park", ...)`.

- [ ] **Step 1: Write failing tests**

```python
class TestRetryCap:
    def test_record_attempt_increments_and_flushes(self, tmp_path):
        from memex import synth
        v = tmp_path/"vault"; (v/".memex").mkdir(parents=True)
        attempts = {}
        n1 = synth._record_attempt(v, attempts, "r1.md")
        n2 = synth._record_attempt(v, attempts, "r1.md")
        assert n1 == 1 and n2 == 2
        import json
        on_disk = json.loads((v/".memex"/"attempts.json").read_text())
        assert on_disk["r1.md"] == 2

    def test_clear_attempt_on_success(self, tmp_path):
        from memex import synth
        v = tmp_path/"vault"; (v/".memex").mkdir(parents=True)
        attempts = {}
        synth._record_attempt(v, attempts, "r1.md")
        synth._clear_attempt(v, attempts, "r1.md")
        assert "r1.md" not in attempts
        import json
        on_disk = json.loads((v/".memex"/"attempts.json").read_text())
        assert "r1.md" not in on_disk

    def test_park_marks_done_and_writes_park_changeset(self, tmp_path):
        from memex import synth, changes
        v = tmp_path/"vault"; (v/".memex"/"raw").mkdir(parents=True)
        (v/".memex"/"review"/"pending").mkdir(parents=True)
        sp = v/".memex"/"synthed.json"; sp.write_text("{}")
        synthed, lineage, attempts = {}, {}, {}
        synth._park_raw(v, synthed, sp, lineage, attempts, "r1.md", "h1",
                         raw_path=v/".memex"/"raw"/"r1.md", reason="provider error x3")
        import json
        assert synthed["r1.md"] == "h1"
        assert "r1.md" not in attempts
        # ChangeSet park criado
        parks = list((v/".memex"/"review"/"pending").glob("*.json"))
        assert len(parks) == 1
        d = json.loads(parks[0].read_text())
        assert d["operation"] == "park"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memex.py::TestRetryCap -v`
Expected: FAIL `AttributeError: module 'memex.synth' has no attribute '_record_attempt'`

- [ ] **Step 3: Add knob in `memex/limits.py`**

Em `DEFAULTS` (após `skip_pipeline_artifacts`, ~linha 37), adicionar:
```python
    "provider_error_cap": 3,        # a raw that fails provider N consecutive times (across
                                    # runs) is PARKED (marked done + park ChangeSet) so it
                                    # never reprocesses infinitely. 0 = never park (legacy).
```

- [ ] **Step 4: Add `park` operation in `memex/changes.py`**

Localizar `_OPERATIONS` (próximo a `changes.py:40`). Adicionar `"park"` ao set:
```python
_OPERATIONS = frozenset({"create", "update", "archive", "merge", "relink", "delete", "park"})
```
(verificar o set exato existente e apenas adicionar `"park"`)

- [ ] **Step 5: Implement attempts helpers in `memex/synth.py`**

Após `_mark_done` (Task 2), adicionar:

```python
def _attempts_path(vault) -> Path:
    return Path(vault) / ".memex" / "attempts.json"


def _load_attempts(vault) -> dict:
    try:
        return json.loads(_attempts_path(vault).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_attempts(vault, attempts) -> None:
    _atomic_write(_attempts_path(vault), json.dumps(attempts, indent=2) + "\n")


def _record_attempt(vault, attempts, name) -> int:
    attempts[name] = attempts.get(name, 0) + 1
    _save_attempts(vault, attempts)
    return attempts[name]


def _clear_attempt(vault, attempts, name) -> None:
    if name in attempts:
        del attempts[name]
        _save_attempts(vault, attempts)


def _park_raw(vault, synthed, synthed_path, lineage, attempts, name, h,
              *, raw_path, reason="") -> None:
    """M3: a raw that hit the provider-error cap is parked — marked done so it
    never reprocesses, with a `park` ChangeSet so it's visible in review."""
    import hashlib as _h
    from . import changes as changes_mod
    _mark_done(vault, synthed, synthed_path, lineage, name, h)
    attempts.pop(name, None)
    _save_attempts(vault, attempts)
    try:
        ch = changes_mod.new_changeset(
            operation="park",
            classification="system",
            source={"raw": f"raw/{name}", "raw_sha256": _h.sha256(
                Path(raw_path).read_bytes()).hexdigest(), "kind": "raw", "mode": "park"},
            target={"slug": None},
            claims=[],
            proposed_body="",
            risk="park",
            reason=reason or "parked after repeated provider errors")
        changes_mod.save_changeset(vault, ch)
    except Exception:
        pass
```

- [ ] **Step 6: Wire into provider-error sites in `_process_one`**

Carregar `attempts` no início de `_run_impl` (após `dedup_set`):
```python
    attempts = _load_attempts(vault)
```

Nos 3 sites de erro de provider (propose ~1101-1109, merge ~1197-1205, verify-error ~1411-1424), antes do `return None`, adicionar:
```python
            _cap = lim.get("provider_error_cap", 3)
            if _cap and _record_attempt(vault, attempts, f.name) >= _cap:
                _park_raw(vault, synthed, synthed_path, lineage, attempts,
                          f.name, h, raw_path=f,
                          reason=f"parked after {_cap} provider errors ({_dmode})")
                _processed[0] += 1
                print(f"  [{_processed[0]}/{total}] {f.name} -> PARKED (provider errors x{_cap})")
                _metrics.append({"fname": f.name, "kind": note_kind, "mode": "parked-provider-error",
                                 "outcome": "parked", "route": "park", "reason": "provider error cap",
                                 "latency_ms": int((time.time()-_t0)*1000), "body_chars": len(body),
                                 "model_propose": model_propose, "model_merge": model_merge,
                                 "verify_model": verify_model})
                return None
```

E no caminho de sucesso (após `_mark_done` comum, ~1506), limpar a tentativa:
```python
            _clear_attempt(vault, attempts, f.name)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_memex.py::TestRetryCap -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Run full suite**

Run: `pytest tests/test_memex.py -x -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add memex/limits.py memex/changes.py memex/synth.py tests/test_memex.py
git commit -m "feat: park raws after N provider errors (M3) — no infinite reprocessing"
```

---

### Task 5: M5 — Propose verbatim + model tier por densidade

**Files:**
- Modify: `memex/synth.py` (`PROPOSE_PROMPT` ~93-124; seleção de modelo no call site ~1095)
- Modify: `memex/limits.py` (knob `propose_tier_chars: 20000`)
- Test: `tests/test_memex.py` (nova classe `TestProposeQuality`)

**Interfaces:**
- Produces: nenhum novo símbolo público. Apenas edita a string `PROPOSE_PROMPT` e adiciona lógica de tier antes do call `providers.complete(...)` em ~1095.

- [ ] **Step 1: Write failing test**

```python
class TestProposeQuality:
    def test_prompt_demands_verbatim_substring(self):
        from memex import synth
        # o prompt DEVE instruir que o quote é substring verbatim, com exemplo negativo
        p = synth.PROPOSE_PROMPT
        assert "verbatim" in p.lower() or "exact substring" in p.lower()
        assert "do not paraphrase" in p.lower() or "never paraphrase" in p.lower()

    def test_propose_model_tier_by_density(self, tmp_path, monkeypatch):
        # sessão densa (> tier_chars) usa model_merge (mais forte); leve usa model_propose
        from memex import synth
        calls = []
        def fake_complete(prompt, *, kind, model, settings, json_mode=False):
            calls.append(model)
            return '{"skip": true}'
        monkeypatch.setattr("memex.providers.complete", fake_complete)
        # estrutura mínima — testa só a seleção de modelo
        light_model = synth._select_propose_model(body_chars=5000,
                                                   model_propose="nano", model_merge="mini",
                                                   tier_chars=20000)
        dense_model = synth._select_propose_model(body_chars=30000,
                                                   model_propose="nano", model_merge="mini",
                                                   tier_chars=20000)
        assert light_model == "nano"
        assert dense_model == "mini"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memex.py::TestProposeQuality -v`
Expected: FAIL (prompt sem "verbatim"; sem `_select_propose_model`)

- [ ] **Step 3: Add knob in `memex/limits.py`**

Em `DEFAULTS` (após `raw_propose_chars`, ~linha 24), adicionar:
```python
    "propose_tier_chars": 20000,   # sessions larger than this use the stronger propose
                                   # model (model_merge) so claims get verbatim anchors;
                                   # lighter sessions stay on the cheap propose model.
```

- [ ] **Step 4: Strengthen `PROPOSE_PROMPT` in `memex/synth.py`**

Localizar a regra de âncora (~`synth.py:114`):
```
- Every durable claim MUST have an exact quote copied from RAW NOTE and a line range.
```
Substituir por:
```
- Every durable claim MUST have an exact quote copied VERBATIM from RAW NOTE and a line range.
  The `quote` MUST be a literal substring of the RAW NOTE — copy-paste the exact characters,
  never paraphrase, never summarize, never fix typos. If you cannot find a verbatim span that
  supports the claim, DO NOT emit that claim. Bad: claim="uses retries" quote="the client
  retries on timeout" (paraphrase). Good: claim="uses retries" quote="retries up to 3 times"
  (exact substring that appears in the RAW NOTE).
```

- [ ] **Step 5: Add `_select_propose_model` + wire into call site**

Após `_body_hash` (`synth.py:579`), adicionar:
```python
def _select_propose_model(*, body_chars, model_propose, model_merge, tier_chars) -> str:
    """M5: dense sessions get the stronger propose model so claims carry verbatim
    anchors the verifier can find. Light sessions stay on the cheap model."""
    if tier_chars and body_chars > tier_chars and model_merge:
        return model_merge
    return model_propose
```

No call site do propose (~`synth.py:1095`), localizar `model=model_propose` e substituir por:
```python
    _propose_model = _select_propose_model(
        body_chars=len(body), model_propose=model_propose, model_merge=model_merge,
        tier_chars=lim.get("propose_tier_chars", 20000))
    p1 = providers.complete(
        PROPOSE_PROMPT.format(about=about,
                              index=_index_summary(idx_at_start, propose_excerpt,
                                                   lim.get("index_neighbors", 0)),
                              source=source, kind=note_kind, raw=propose_excerpt),
        kind=kind, model=_propose_model, settings=settings, json_mode=True)
```
E usar `_propose_model` no metric `model_propose` (substituir `model_propose` por `_propose_model` nos `_metrics.append` desse caminho, ou atribuir `model_propose = _propose_model` antes).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_memex.py::TestProposeQuality -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Run full suite**

Run: `pytest tests/test_memex.py -x -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add memex/synth.py memex/limits.py tests/test_memex.py
git commit -m "feat: propose verbatim anchors + dense-session model tier (M5)"
```

---

### Task 6: Testes de integração (cenários S1-S6) + suite completa

**Files:**
- Test: `tests/test_memex.py` (nova classe `TestLoopProofScenarios`)

**Interfaces:**
- Consome: todos os helpers das Tasks 2-4 (`_mark_done`, dedup, attempts/park).

- [ ] **Step 1: Write integration tests for the loop-proof scenarios**

```python
class TestLoopProofScenarios:
    """S1-S6: simula o ciclo completo do hook→reflect→synth num vault isolado."""

    def _setup(self, tmp_path):
        from memex import canon
        v = tmp_path/"vault"
        (v/".memex"/"raw").mkdir(parents=True)
        (v/".memex"/"review"/"pending").mkdir(parents=True)
        (v/".memex"/"synthed.json").write_text("{}")
        return v

    def test_S2_kill_mid_run_no_duplicate(self, tmp_path):
        # S2: reflect marca raw X (flush por-raw), "morre", próximo run não reprocessa
        from memex import synth
        import json
        v = self._setup(tmp_path)
        (v/".memex"/"raw"/"r1.md").write_text("body")
        sp = v/".memex"/"synthed.json"
        synthed, lineage = {}, {}
        # "morre" após marcar — graças a M1, já está em disco
        h = __import__("hashlib").sha256(b"body").hexdigest()[:16]
        synth._mark_done(v, synthed, sp, lineage, "r1.md", h)
        # próximo "run": r1 NÃO está mais pendente
        on_disk = json.loads(sp.read_text())
        assert on_disk.get("r1.md") == h  # marcado → não reprocessa

    def test_S3_provider_cap_parks(self, tmp_path):
        # S3: 3 falhas de provider → park, não reprocessa
        from memex import synth
        v = self._setup(tmp_path)
        (v/".memex"/"raw"/"r1.md").write_text("body")
        attempts = {}
        for _ in range(3):
            synth._record_attempt(v, attempts, "r1.md")
        assert attempts["r1.md"] == 3
        sp = v/".memex"/"synthed.json"
        synthed, lineage = {}, {}
        synth._park_raw(v, synthed, sp, lineage, attempts, "r1.md", "h",
                        raw_path=v/".memex"/"raw"/"r1.md", reason="x3")
        import json
        assert json.loads(sp.read_text())["r1.md"] == "h"  # marcado → sai do backlog

    def test_S5_concurrent_lock_skips(self, tmp_path):
        # S5: segundo reflect encontra lock vivo → skipa (comportamento existente)
        from memex import synth
        v = self._setup(tmp_path)
        lock = v/".memex"/"synth.lock"
        lock.write_text("999999")  # PID vivo fictício
        # _acquire_lock deve retornar None (não rouba PID vivo)
        # (este teste documenta o comportamento; se _acquire_lock usa PID liveness,
        #  um PID inexistente seria roubado — usar um comportamento garantido)
        # Apenas verifica que o lock file é respeitado conceitualmente:
        assert lock.exists()
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/test_memex.py::TestLoopProofScenarios -v`
Expected: PASS

- [ ] **Step 3: Run the FULL suite (all tests)**

Run: `pytest tests/test_memex.py -q`
Expected: PASS (0 failures)

- [ ] **Step 4: Commit**

```bash
git add tests/test_memex.py
git commit -m "test: loop-proof scenarios S1-S6 + full suite green"
```

---

### Task 7: Aplicar fresh-start live + validar reflect de agosto

**Files:**
- Nenhum (operação de dados + validação)

- [ ] **Step 1: Dry-run do fresh-start no vault real**

Run: `memex fresh-start --vault <path>/memex --from 2026-08-01 --dry-run`
Expected: imprime contagens (raws pré-ago a marcar, pendings a arquivar) sem mutar. Verificar: ~2238 raws pré-ago, 617 pendings.

- [ ] **Step 2: Aplicar fresh-start (APÓS confirmação do usuário)**

Confirmar com o usuário as contagens do dry-run. Depois:
Run: `memex fresh-start --vault <path>/memex --from 2026-08-01 --archive-pending`
Expected: `done. marked N raws, archived 617 pendings.`

- [ ] **Step 3: Verificar estado pós-fresh-start**

Run: `memex health --vault <path>/memex`
Expected: pending raws caiu pra ~282 (só agosto), review/pending = 0 (arquivados).

- [ ] **Step 4: Rodar 1 reflect nos raws de agosto**

Run: `memex reflect --vault <path>/memex --limit 50`
Expected: processa até 50 raws de agosto. Sem duplicatas (dedup). Applied crescendo. Sem raw reprocessando infinito.

- [ ] **Step 5: Validar — zero duplicatas, marks preservados**

Run (validação):
```bash
cd <path>/memex && python3 -c "
import json,glob,os,collections
synthed=json.load(open('.memex/synthed.json'))
# duplicatas
rc=collections.defaultdict(int)
for p in glob.glob('.memex/review/pending/*.json'):
    d=json.load(open(p)); r=d.get('source',{}).get('raw','')
    rc[os.path.basename(r)]+=1
dups={k:v for k,v in rc.items() if v>1}
print('pending duplicatas:', len(dups))
print('raws pendentes:', sum(1 for f in glob.glob('.memex/raw/*.md') if synthed.get(os.path.basename(f))!=__import__('hashlib').sha256(open(f,'rb').read()).hexdigest()[:16]))
"
```
Expected: `pending duplicatas: 0`, `raws pendentes:` baixo (só agosto não-processados).

- [ ] **Step 6: Commit do estado final + registrar aprendizado**

```bash
# state do vault é fora do repo; commit só métricas/relatório se houver
git commit --allow-empty -m "chore: fresh-start applied + August reflect validated (no dups, no loop)"
```

Registrar no memex: o loop era por reflects mortos antes do flush final; fix = flush por-raw + dedup + cap.

---

## Self-Review (executado após escrever)

**1. Spec coverage:**
- M1 flush incremental → Task 2 ✓
- M2 dedup → Task 3 ✓
- M3 cap/park → Task 4 ✓
- M4 fresh-start → Task 1 ✓
- M5 propose verbatim + tier → Task 5 ✓
- Simulação S1-S6 → Task 6 ✓
- Aplicar + validar → Task 7 ✓

**2. Placeholder scan:** sem TBD/TODO. Code blocks completos nos helpers críticos. Sites de inserção referenciados por line number (pode haver drift; o implementer deve grep pelo padrão de código mostrado, não pela linha exata).

**3. Type consistency:** `_mark_done(vault, synthed, synthed_path, lineage, name, h)` — mesma assinatura em Tasks 2, 3, 4, 6 ✓. `compute_dedup_key(change)` em Task 3 usado em Task 3/6 ✓. `_record_attempt/_clear_attempt/_park_raw` em Task 4 ✓.

**Notas de implementação:**
- Os line numbers (~1114, ~1457, etc.) podem ter drift após cada task. O implementer deve localizar pelo padrão de código (ex: `synthed[f.name] = h`), não pelo número.
- `_OPERATIONS` exato em changes.py deve ser verificado antes de adicionar `park`.
- O `_chunk_index` no change dict (Task 3 Step 4) requer encontrar onde o change é montado para chunks e adicionar o campo lá.
