# Runbook de migração — Wiki Integrity

Este runbook é o procedimento operacional para aplicar a migração **wiki-integrity**
num vault real (ex.: `~/memex`), com snapshot, dry-run, aprovação humana, aplicação
de um lote e rollback.

> **HUMAN-GATE (obrigatório).** As **Steps 4–6 do plano** (baseline read-only no
> vault real e aplicação de um lote aprovado) **estão pendentes de autorização
> explícita do usuário humano** e **não devem ser automatizadas**. Este runbook
> documenta os comandos; a execução contra dados reais exige o OK do humano,
> lote por lote. Nenhum comando sem `--dry-run` pode rodar contra o vault real
> antes disso.

---

## 0. Pré-requisitos

- `memex` instalado (`uv tool install -e .` ou o venv do projeto).
- O vault real a ser migrado (ex.: `/Users/fulano/memex`).
- Um snapshot completo do vault **antes** de qualquer mutação.

---

## 1. Snapshot do vault inteiro

O `.memex/` é gitignorado — o git **não** consegue tirar snapshot dele. Por isso o
snapshot é um tar/cp do vault inteiro, com carimbo de data/hora no nome. O snapshot
é a rede de segurança para qualquer passo de rollback.

```bash
VAULT="/absolute/path/to/vault"
STAMP="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT="${VAULT%/}-before-wiki-integrity-${STAMP}.tar.gz"
tar -C "$(dirname "$VAULT")" -czf "$SNAPSHOT" "$(basename "$VAULT")"
```

O `tar -C "$(dirname "$VAULT")" ... "$(basename "$VAULT")"` garante que o tarball
contenha a pasta do vault (incluindo `.memex/`) com caminhos relativos, e não um
caminho absoluto.

---

## 2. Baseline read-only (dry-run) — autorizado apenas com OK humano

Rode **todos** os lotes em dry-run. Dry-run **nunca** toca em `wiki/` nem aplica
nada: lot 0 só lista, lot 1 só gera ChangeSets pendentes de reclassificação, lot 2
só arquiva candidatos pendentes. O relatório vai para `.memex/audit/latest.{md,json}`
e para o stdout.

```bash
memex health --vault "$VAULT"
memex audit --vault "$VAULT" --dry-run --lot 0
memex audit --vault "$VAULT" --dry-run --lot 1
memex audit --vault "$VAULT" --dry-run --lot 2
```

### Regra de ouro

> **Nenhum `memex audit` sem `--dry-run` é executado até que um humano inspecione
> os relatórios e selecione explicitamente um lote.** A aplicação é sempre um passo
> separado, posterior à leitura do relatório e à escolha humana.

---

## 3. O que cada lote faz

| Lote | O que é | Comportamento em dry-run | Comportamento sem dry-run |
|---|---|---|---|
| **0** | Artefatos gerados legados (views de máquina) migrados de layout para baixo de `.memex/views` / `.memex/audit` | Lista os artefatos e os arquivos `_*.md` desconhecidos; **não move nada** | Migração **journaled**: move os bytes para o destino `.memex/`, remove o caminho legado e grava um evento `migrate-artifact` no `transactions.jsonl` (bytes recuperáveis). Não é ChangeSet — é layout, não mutação de conhecimento |
| **1** | Identidades técnicas — páginas com slug `note-*` ou identidades não-canônicas | Gera ChangeSets `reclassify` com risk `review` pendentes | **Nunca auto-aplicado**: arquiva os ChangeSets como pendentes de revisão humana. Só um `memex review approve` aplica |
| **2** | Duplicatas mecânicas (mesmo slug + conteúdo equivalente) | Gera ChangeSets `merge` pendentes | Aplica automaticamente via `changes.apply_changeset`: **merge com histórico** (originais vão para `.memex/history/wiki/`) e **reescrita de backlinks** |

**Fora deste incremento:** lotes de identidade semântica e de arquivamento de
conteúdo não suportado **NÃO** estão nesta entrega — não há comando que os rode.

---

## 4. Aplicar um lote aprovado (e exercitar o rollback)

Após o humano **inspecionar os relatórios** e **escolher exatamente um lote**
(ex.: `<approved-lot>` = `0` ou `2`), o fluxo aplicar → verificar → rollback é:

```bash
memex audit --vault "$VAULT" --lot <approved-lot>
memex health --vault "$VAULT"
memex review rollback <one-change-id> --vault "$VAULT"
memex health --vault "$VAULT"
```

- `memex audit --vault "$VAULT" --lot <approved-lot>`: aplica **somente** o lote
  escolhido (o valor de `<approved-lot>` vem da decisão humana; o relatório
  dry-run anterior mostra os IDs de ChangeSet candidatos).
- `memex health --vault "$VAULT"`: confirma o estado pós-aplicação (contagens de
  páginas canônicas, fila de revisão, identidades inválidas).
- `memex review rollback <one-change-id> --vault "$VAULT"`: reverte **um** ChangeSet
  aplicado a partir do seu manifest de transação (restaura arquivo/índice/links).
- `memex health --vault "$VAULT"`: confirma o estado restaurado.

> Vale repetir: este bloco só roda depois da aprovação humana explícita. Não
> continue para lotes de identidade semântica ou arquivamento de conteúdo não
> suportado na mesma janela de mudança.

---

## 5. Checklist do operador

1. [ ] Snapshot criado (`before-wiki-integrity-<STAMP>.tar.gz`) e conferido.
2. [ ] Baseline read-only rodado (health + audit dry-run lot 0/1/2).
3. [ ] Relatório `.memex/audit/latest.md` lido por um humano.
4. [ ] **Um** lote selecionado e autorizado explicitamente.
5. [ ] Lote aplicado (`memex audit --lot <approved-lot>`).
6. [ ] `memex health` pós-aplicação conferido.
7. [ ] Rollback exercitado em um ChangeSet (`memex review rollback <id>`) e health confirmado.

---

## 6. Referência do plano

- Plano completo: `docs/superpowers/plans/2026-08-06-wiki-integrity.md`
- O presente runbook cobre as Steps 1–3 e 7. As Steps 4–6 (baseline read-only no
  vault real, aprovação de lote e aplicação + rollback) **aguardam autorização
  humana explícita** — ver HUMAN-GATE acima.
