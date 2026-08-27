# Memex loop-proof pipeline — design

**Data:** 2026-08-10
**Status:** aprovado
**Autor:** memex maintainers (via brainstorming)

## Contexto e problema

A pipeline de síntese do memex (`reflect` → `synth`) apresentava um loop de reprocessamento que acumulava ChangeSets duplicados e impedia o backlog de drenar.

### Diagnóstico (verificado no código + estado real)

O caminho de `review` **não** é o loop — ele marca o raw como processado (`synth.py:1504`), para ambos os routes (`auto_apply` e `review`/`pending`). O loop tem 3 causas-raiz reais:

1. **Sem flush incremental** — `synth.json` só é gravado no fim do run (`synth.py:1557`, dentro do `finally`). Um reflect morto (exit/kill/crash/circuit-breaker) antes do fim perde todas as marcas em memória, mas os ChangeSets já foram gravados em disco (escrita atômica por-id durante o run).
2. **Sem dedup na criação do ChangeSet** — o mesmo `(raw, slug, section, chunk_idx)` reprocessado gera um novo ChangeSet em vez de superseder o existente.
3. **Reflects nunca completam** — exits/compacts frequentes disparam reflects que são mortos pelo próximo exit antes de terminar os 300 raws/run.

### Prova concreta

- 160 raws têm ChangeSets pending duplicados (pior caso: 21 cópias do mesmo raw).
- O slice `langfuse-observability-export-dab` tem **11 ChangeSets pending idênticos** gerados em **11 runs distintos** (11 `created_at` diferentes em ~3h).
- 255 de 267 raws-com-pending NÃO estão marcados em `synthed.json` → todos reprocessam a cada reflect.

### Estado atual

| Métrica | Valor |
|---|---|
| raws pendentes (reprocessam) | 2520 (1 mai, 268 jun, 1969 jul, 282 ago) |
| pending ChangeSets (review) | 617 |
| duplicatas (mesmo raw, >1 pending) | 160 raws |
| raws c/ pending mas não marcados | 255 |
| applied / rejected / stale | 404 / 477 / 17 |

### Decisão do usuário

Fresh start: **agosto pra frente**. Marca raws pré-01-ago como processados (sem sintetizar), arquiva os 617 pending duplicados, preserva os 404 applied (já são wiki). Recomeça limpo processando só agosto.

## Objetivos

1. Eliminar qualquer possibilidade de reprocessamento infinito (raw nunca processa mais do que o necessário).
2. Drenar o backlog herdado via fresh start.
3. Comportamento daqui pra frente: crescimento da wiki com precisão (Approach A — precision-first).
4. Observável e simulável.

## Arquitetura — 5 mudanças

### M1. Flush incremental do `synth.json` + lineage

**Onde:** `memex/synth.py` (`_flush_state`, `_process_one`).

Hoje `_flush_state` é chamado uma vez no `finally` (`synth.py:1557`) com flag `_synthed_dirty`. Passa a gravar `synth.json` e `lineage.json` **a cada raw** duravelmente tratado, dentro do bloco já protegido por `write_lock` (após `synth[f.name] = h` em `synth.py:1504` e análogos: skip meta-worker 746, superseded 776, skip-ids 793, delta-vazio 820, prop.skip 1114, auto-reject 1436, chunk-done 1549).

- Extrair `_flush_synthed(vault, synthed, synthed_path)` e `_flush_lineage(vault, lineage)` como helpers atômicos reutilizáveis.
- Chamá-los após cada marcação durável (raw marcado = flush imediato).
- Views (embeddings index) e metrics continuam batch no fim (idempotentes / só observabilidade).
- Custo: ~1ms de IO atômico por raw, sob write_lock. Aceitável (raws/run ~100, 4 workers).

**Garante:** um reflect morto a qualquer momento preserva todas as marcas até o último raw completado.

### M2. Dedup na criação do ChangeSet

**Onde:** `memex/synth.py` (antes de `_write_changeset`) + helper de carregamento do set.

- No início do `synth.run`, carregar um `dedup_set`: mapping `dedup_key → change_id` a partir de `.memex/review/pending/*.json`.
- `dedup_key = hash(raw_sha256, slug, section, chunk_idx, operation)`. Para não-chunk, `chunk_idx = None`.
- Antes de criar um ChangeSet:
  - Se `dedup_key` existe em pending:
    - Se `proposed_body` hash bate → **skip** (já pending, idêntico), marcar raw processado, métrica `mode: dedup-skip`.
    - Se differ → marcar o pending antigo como `stale` (mover p/ `review/stale/`), criar novo, atualizar `dedup_set`.
  - Senão → criar normalmente, adicionar ao `dedup_set`.
- **Crítico:** ao encontrar pending existente, o raw é marcado como processado (o trabalho já está em review) — sai do backlog mesmo sendo reprocesso.

**Garante:** reprocessar um raw nunca gera duplicata; o slice já-pending faz o raw sair do backlog.

### M3. Cap de retry por raw (park)

**Onde:** `memex/synth.py` + arquivo `.memex/attempts.json`.

- `attempts.json`: `{raw_basename: consecutive_failures}`. Carregado no início do run, gravado atomicamente em cada falha.
- Em cada falha de provider (propose/merge/verify-error) para um raw: incrementar `attempts[raw]`.
- Se `attempts[raw] >= 3`: **park** — marcar `synthed[raw] = hash`, métrica `mode: parked-provider-error`, criar ChangeSet `operation: park` em pending (visível no review para re-trigger manual), remover de `attempts`.
- Se o raw eventualmente suceder: zerar `attempts[raw]`.
- Reset global opcional: `memex unpark --raw <name>` remove a marca p/ reprocessar.

**Garante:** raw que falha persistentemente (ex.: sessão malformada) não é reprocessado infinitamente.

### M4. Fresh start one-time

**Onde:** novo subcomando `memex fresh-start --vault <path> --from YYYY-MM-DD [--dry-run] [--archive-pending]`.

- Lista raws cujo prefixo de data `< --from`. Marca todos como processados em `synth.json` (hash real do arquivo), sem chamar LLM.
- Move os pending ChangeSets p/ `.memex/review/archived-pre-freshstart/` (preserva como evidência, sai da fila de review).
- Preserva `applied/` e `rejected/` intactos (applied já é wiki).
- `--dry-run`: imprime contagens (raws a marcar por mês, pendings a arquivar) sem mutar.
- Idempotente: re-rodar não faz nada já feito.

**Garante:** backlog herdado zerado, só agosto processa daqui pra frente.

### M5. Propose quality (Approach A — precision-first)

**Onde:** `memex/synth.py` (`PROPOSE_PROMPT`) + tier de modelo.

Raiz do 93% review: 417 pendings com `"claim text not found in source raw"` — o modelo de propose (gpt-5-nano) gera claims cuja citação não é verbatim no raw, então o verify não ancora.

Dois travões:

1. **Prompt tightening:** instruir o propose a extrair o texto da claim **literalmente do raw** (quote verbatim). O `anchor` deve ser uma substring exata existente no `source_text`. Proibir paráfrase como anchor. Adicionar exemplos few-shot de anchor válido vs inválido.
2. **Model tier por densidade:** sessões densas (`body_chars > DENSE_THRESHOLD`, default = 20000, configurável em `limits.propose_tier_chars` OU entidades detectadas na propose) sobem de gpt-5-nano → gpt-5-mini para propose. Custo um pouco maior, mas claims ancoráveis. Nano mantido para sessões leves.

Métrica nova: `propose_model_tier` (nano/mini) e `anchors_found_ratio` p/ acompanhar a melhoria.

**Garante:** review rate cai de ~93% → ~45%, applied cresce, sem sacrificar precisão (verify continua estrito).

## Data flow (forward, agosto+)

```
SessionEnd hook (exit)
  └─ memex capture (rápido: escreve raw + ingere docs)
       └─ spawn detached: memex reflect --vault ...
            └─ synth.run (adquire .memex/synth.lock; skipa se outro rodando)
                 ├─ carrega synthed.json + lineage + dedup_set + attempts.json
                 ├─ todo = raws onde synthed[raw] != hash (max 300)
                 ├─ for each raw (paralelo, 4 workers, write_lock p/ estado):
                 │    ├─ _prepare_todo: skip meta-worker/superseded/skip-ids
                 │    │    → mark synthed + flush imediato (zero LLM)
                 │    ├─ doc deterministic route → mark + flush (zero LLM)
                 │    ├─ else: propose (tier por densidade) → merge → verify
                 │    │    ├─ dedup check: pending existe p/ slice?
                 │    │    │    → mark synthed + flush, skip create (M2)
                 │    │    ├─ auto_apply → apply_changeset → mark + flush
                 │    │    ├─ review → write pending ChangeSet → mark + flush
                 │    │    └─ reject → mark + flush
                 │    ├─ provider error → attempts[raw]++; if >=3 → park (M3)
                 │    └─ (todo raw marcado: synthed+lineage flushed em disco)
                 └─ fim: flush views + metrics
```

## Simulação de cenários

- **S1 exit normal:** raw novo → propose verbatim → verify supported → auto_apply → flush → done. Próximo exit não reprocessa.
- **S2 exit mata reflect:** flush por-raw garante que raws já tratados estão em disco. Mesmo morte entre ChangeSet-write e synthed-mark → dedup (M2) acha pending, marca, não duplica.
- **S3 provider down persistente:** 3 falhas consecutivas → park. Não reprocessa infinito.
- **S4 PreCompact:** `--partial`, não spawna reflect.
- **S5 dois exits concorrentes:** lock (`synth.lock`) faz o segundo pular.
- **S6 chunked longo:** chunks processados independentes; chunk-done só quando todos slices ok; dedup evita duplicar slice já-pending.

## Erro handling

- Kill/crash: M1 (flush por-raw) preserva estado.
- Provider error transitório: M3 (cap 3) evita loop; usuário re-trigger via `unpark`.
- Concorrência: lock existente + writes atômicos.
- ChangeSet órfão (pending de raw já marcado): M2 dedup trata na reentrada.
- Fresh start idempotente: M4 re-rodar é no-op.

## Testes

- **Unit:** dedup_key, flush chamado por-raw, cap de retry (3 falhas → park), fresh-start marking, model tier por densidade.
- **Integração (test vault isolado):** S1–S6 como testes reprodutíveis.
- **Suite existente:** `tests/test_memex.py` + novos testes em `TestLoopProofPipeline`.
- **Golden set:** comparar decisões de agosto vs referência.
- **Repro isolado:** NUNCA rodar repro com mock provider no vault real (memo: `never-run-repro-mocks-on-real-vault`).

## Ordem de implementação

1. `memex fresh-start` CLI + `--dry-run` (ver contagens antes de aplicar).
2. Aplicar fresh-start (marca pré-ago, arquiva 617 pendings).
3. M1 flush incremental + M2 dedup + M3 cap (com testes).
4. M5 tighten PROPOSE_PROMPT + model tier.
5. 1 reflect nos raws de agosto → validar: zero duplicatas, applied crescendo, sem raw reprocessando.
6. Habilitar para hooks live (já estão wired; só validar comportamento).

## Não-escopo (YAGNI)

- Redesign do verify (já é body-fidelity p/ chunks/delta/docs; sessão full mantém gate estrito — Approach A).
- Novo formato de ChangeSet (schema atual é suficiente; só adiciona `operation: park`).
- Backfill dos 3k snapshots como delta (fora de escopo; fresh start torna irrelevante).
- UI de review (CLI `memex review list` já existe).
