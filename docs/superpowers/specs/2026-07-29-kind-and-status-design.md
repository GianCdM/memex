# Memex — Substituir `tier` por `kind` + `status` + changelog

**Data:** 2026-07-29
**Status:** aprovado, aguardando implementação

---

## Contexto

O sistema atual de `tier` (bronze/silver/gold) foi emprestado da arquitetura medallion do Databricks — um paradigma de pipeline de dados, não de conhecimento pessoal. Na prática, só dois tiers têm comportamento diferente (silver = sobrescrito livremente; gold = snapshot antes de sobrescrever), e bronze é puramente cosmético. O modelo não responde às perguntas reais do dono do cérebro: "de onde veio essa informação?", "essa decisão ainda vale?", "o que mudou desde a última vez que vi?".

## Design

Três conceitos substituem o sistema de tiers:

### 1. `kind` — de onde veio (frontmatter + corpo)

Substitui `tier`. Puramente informativo — zero lógica condicional baseada nele.

**Valores:** `session | doc | manual | code | merged`

**Onde aparece:**
- **Frontmatter:** `kind: session`
- **Corpo da página:** blockquote de abertura com emoji e label:
  - `> 💬 Sessão de IA`
  - `> 📄 Documento`
  - `> ✍️ Salvo manualmente`
  - `> 🏛️ Código`
  - `> 🔀 Consolidado`

**Atribuição:** definida na ingestão, sem intervenção do LLM:

| Origem | `kind` | Quem define |
|---|---|---|
| Sessão de IA (Claude, Cursor, Codex) | `session` | `capture.py` / `ingest.py` |
| Documento importado (PDF, PPTX, MD) | `doc` | `ingest.py` |
| `memex remember` | `manual` | `mcp_server.py` → `ingest.py` |
| `memex analyze` | `code` | `analyze.py` |
| Tidy/gardening (merge de duplicates) | `merged` | `gardening.py` |

**Precedência no merge:** quando múltiplas raw notes alimentam a mesma wiki page, ganha o `kind` de maior força: `manual > code > doc > session > merged`. Uma página que recebeu um `remember` nunca perde o selo `manual`, mesmo que sessões subsequentes adicionem conteúdo.

### 2. `status` — ainda vale? (frontmatter + campo auxiliar)

Novo campo no frontmatter. Default é `current`. O LLM pode propor alteração no merge quando detecta revogação ou obsolescência. Também editável à mão no Obsidian.

**Valores:**

| Status | Significado | Comportamento |
|---|---|---|
| `current` | Vigente. É a palavra final. | Padrão para toda página nova. |
| `superseded` | Substituído por decisão/página mais recente. | Campo `superseded_by: [[outra-pagina]]` obrigatório. |
| `obsolete` | Não vale mais e não foi substituído. Projeto morreu, time dissolvido. | Informativo. |
| `deprecated` | Ainda tem valor histórico, mas a recomendação mudou. | Informativo. |
| `archived` | Correto, só não é mais relevante pro dia a dia. | Informativo. |
| `draft` | Incompleto, não revisado. | Informativo. |

**Campo auxiliar:** `superseded_by` — wikilink para a página substituta (obrigatório quando `status: superseded`).

**Regra:** `status` e `superseded_by` são preservados pelo synth — se já existem no frontmatter, não são sobrescritos. O LLM pode propor novos valores, mas nunca apaga um status existente sem evidência explícita no raw note.

### 3. Changelog por página (corpo, automático)

Seção `## 📋 Histórico` no final da página, mantida pelo LLM no merge. Cada entrada resume o que mudou em 1-2 linhas:

```markdown
## 📋 Histórico
- `2026-07-29` — time cresceu de 4 pra 6 pessoas; stack atualizada pra Python 3.12 ([sessão](raw/2026-07-29--memex.md))
- `2026-06-15` — decisão inicial: usar Redis como cache ([sessão](raw/2026-06-15--memex.md))
```

**Regras:**
- Máximo 10 entradas (as mais recentes). Se houver mais, a última linha é `- *(entradas anteriores omitidas)*`.
- Cada entrada linka pro raw note de origem.
- O LLM é instruído a adicionar UMA linha por merge, resumindo o que mudou em relação ao corpo anterior.
- Se nada de substancial mudou (ex.: só correção de formatação), não adiciona linha.

### 4. O que some

- `tier` (bronze | silver | gold) e `TIER_RANK`
- Snapshot de gold em `.memex/history/<slug>/`
- `--tier` da CLI (`choices=["gold", "silver", "bronze"]`)
- `tier` do `PROPOSE_PROMPT` e `_index_summary`
- `tier` do output do MCP `status`
- Seção "Trust tiers" do SCHEMA.md e README

### 5. Migração de dados

**Vault existente:** toda wiki page tem `tier: silver` ou `tier: gold` no frontmatter. A migração:
1. Remove `tier` do frontmatter.
2. Adiciona `kind` baseado no campo `sources` da página:
   - Se `sources` contém `remember:*` → `kind: manual`
   - Se `sources` contém `analyze:*` → `kind: code`
   - Se `sources` contém `doc:*` → `kind: doc`
   - Se a página tem origem em gardening → `kind: merged`
   - Senão → `kind: session`
3. Adiciona `status: current`.
4. Não adiciona `## 📋 Histórico` retroativo.

O diretório `.memex/history/` existente é preservado (não apagado), mas o snapshot de gold deixa de ser gerado.

### 6. Impacto nos arquivos

**Source files a modificar:** `synth.py`, `ingest.py`, `analyze.py`, `gardening.py`, `cli.py`, `mcp_server.py`, `vault.py`, `config.py`, `capture.py`, `search.py`, `init.py`

**Documentação:** `README.md`, `vault.py:SCHEMA_TEMPLATE`

**Vault real:** todas as wiki pages (migração batch via script ou no próximo synth de cada página)

**Testes:** `tests/test_memex.py`, `tests/mock_llm.py`, `tests/live_e2e.sh`

### 7. O que NÃO muda

- Estrutura de diretórios (`raw/`, `workspace/`, `wiki/`)
- Funcionamento do synth (propose → merge → write)
- Funcionamento do reflect, capture, boot, recall
- `log.md` do vault (continua sendo o changelog global)
- `.memex/history/gardening/` (arquivo de páginas absorvidas pelo tidy)

---

## Verificação

1. Testes unitários: 52+ passando, com asserts de `kind` em vez de `tier`
2. MCP `status`: output mostra `kinds` em vez de `tiers`
3. CLI: `--tier` rejeitado, `memex status` mostra `kinds`
4. Página nova: frontmatter tem `kind` e `status`, corpo tem blockquote de origem
5. Merge de página existente: `## 📋 Histórico` ganha nova linha
6. `kind` não afeta comportamento — sem `if kind == "manual"` no código
