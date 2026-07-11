# memex v2 — o redesign ("cérebro ativo")

*2026-07-11. O raciocínio por trás da reescrita, para o eu-do-futuro.*

## Diagnóstico do v1 (por que travou)

1. **Só metade da memória existia.** O v1 tinha memória de longo prazo (wiki via
   synth em lote) e nada de curto prazo. O caso de uso número 1 — abrir uma
   sessão nova e dizer "bora continuar" — falhava por design: o recall era só
   `UserPromptSubmit`, e um prompt de retomada não carrega termos tópicos para
   casar com página nenhuma.
2. **O Claude não sabia que o cérebro existia.** A injeção dizia "abra a página
   no vault" sem caminho; não havia skill, nem SessionStart, nem contrato. O
   wiki era um *produto de pipeline*, não um artefato mantido pelo agente — o
   oposto do modelo LLM-Wiki (Karpathy / bootcamp), onde `SCHEMA.md` + `index.md`
   tornam qualquer agente um leitor/escritor.
3. **Windows quebrado em 5 lugares** (a máquina atual é Windows): hooks com
   `nohup ... &` e `$(date +%F)` (POSIX-only); `os.kill(pid, 0)` **mata** o
   processo no Windows (o lock do synth terminaria um synth vivo); encoding
   cp1252 no stdout/stdin (crash em `→`/`✓`, mojibake em payloads acentuados);
   cwd-encoding do backend claude não cobria `C:\...` (zero sessões capturadas);
   filtro de workspace comparava caminhos com `/`.
4. **Harness subaproveitado.** Sem SessionStart (o único lugar que conserta a
   retomada), sem PreCompact (sessões longas perdiam tudo se morressem), sem
   skill, e o SessionEnd re-escaneava `~/.claude/projects` inteiro em vez de
   usar o `transcript_path` que o hook já entrega.

## As decisões do v2

**Três camadas de memória, mapeadas em quatro eventos do harness:**

| Camada | Arquivo | Evento que escreve | Evento que lê |
|---|---|---|---|
| Episódica (forense) | `raw/` | SessionEnd + PreCompact (`capture`) | synth |
| De trabalho (curto prazo) | `now/<projeto>.md` | `handoff` deliberado ou `reflect` | SessionStart (`boot`) |
| Semântica (longo prazo) | `wiki/` | `reflect`/`synth` | UserPromptSubmit (`recall`) + `search` |

- **`boot` (SessionStart)** injeta a now-page do projeto + ponteiros + instruções
  de uso. É o que faz "continua de onde paramos" funcionar com prompt vazio.
  Silencioso em cérebro vazio e em `source=compact`; exit 0 SEMPRE.
- **`capture` (SessionEnd/PreCompact)** ingere O transcript do payload (sem
  varredura), scrub, ledger; o PreCompact grava parcial e o SessionEnd supersede
  (mesmo id → mesmo arquivo → hash novo → re-synth). O LLM roda só no `reflect`,
  spawnado DESTACADO em Python (DETACHED_PROCESS / start_new_session).
- **`recall` (UserPromptSubmit)** mantém os gates do v1 (overlap + Jaccard), mas
  ranqueia por IDF, deduplica por sessão (`.memex/state/recall-<session_id>`),
  e imprime caminhos absolutos para o modelo poder `Read` a página. Stemming por
  prefixo de 5 chars casa cognatos pt/en (`alertas`~`alerts`) — o cérebro é bilíngue.
- **`now/`**: o handoff deliberado (o Claude escreve o próprio estado via skill —
  ele conhece a sessão melhor que qualquer sumarizador posterior) segura a
  regeneração automática por `now_handoff_hold_hours`. Conhecimento durável
  "gradua" para o wiki via synth; a now-page é sobrescrita, nunca acumulada.
- **Skill de usuário** (`~/.claude/skills/memex/`): o cérebro vira superfície de
  leitura/escrita deliberada — `search` / `remember` / `handoff` — em qualquer
  workspace. Model-invocable ("como decidimos...", "lembra disso", "salva onde
  paramos").
- **`SCHEMA.md` como contrato de agente** (modelo do bootcamp): camadas, tipos de
  página, frontmatter tool-owned, store-vs-skip, supersessão de decisões, e como
  agentes leem/escrevem. + `log.md` append-only (cronologia humana do cérebro).

## O que a pesquisa validou (Perplexity + gist do Karpathy + harness)

- Consenso de produção: camadas híbridas — contexto curto na janela; longo prazo
  externo e estruturado; captura/promoção explícitas; retrieval léxico+grafo;
  reflexão periódica. (Mem0/Letta/Zep/LangMem/generative-agents.)
- Até ~1000 páginas, `index.md` + BM25/keyword + wikilinks **ganham de embeddings**
  em simplicidade e precisão — embeddings ficam para quando comprovadamente
  faltarem. (Por isso recall é IDF-lexical e stdlib.)
- Continuidade de sessão converge para artefatos externos com seções fixas
  (Cline memory-bank, HANDOFF.md): Contexto / Estado / Próximos passos /
  Arquivos-chave — exatamente as seções da now-page.
- Higiene: consolidação como job de primeira classe; supersessão em vez de
  deleção; store-vs-skip curto e literal no prompt (decisões, invariantes,
  padrões recorrentes, preferências — nunca código que vive no git, nunca
  segredos, nunca debugging transitório).
- Harness: SessionStart injeta stdout como contexto; PreCompact existe; skills
  user-level são model-invocable; `claude -p` para o worker headless.

## Validação

- `tests/test_memex.py` — 21 testes in-process, LLM mockado por HTTP local:
  vault ensure/upgrade v1→v2, capture (payload→raw, parcial, supersessão),
  recall (ranking, dedup por sessão, silêncio), boot (injeção, compact, stale),
  handoff/hold, reflect (wiki + now + log), search, hooks (4 eventos, idempotente,
  remove v1, preserva hooks alheios), scrub, pid_alive.
- `tests/live_e2e.sh` — o loop inteiro na máquina real, executando as strings de
  comando INSTALADAS (Git Bash + cmd.exe), com reflect realmente destacado e
  UTF-8 verificado de ponta a ponta.

## v2.1 — manager-first + automação total (feedback do Gian, mesmo dia)

Feedback: "não é só coding — sou manager (gestão/arquitetura/tech-lead, às vezes
código)" e "não gostava da sequência/nomes de comandos; gardening; muita coisa manual".

1. **Zero manutenção.** O `reflect` passou a processar TODO o backlog pendente
   (limitado por `reflect_max_notes` por rodada) — `memex synth` manual saiu do
   fluxo. A consolidação de near-duplicates roda AUTOMÁTICA no reflect a cada
   `tidy_every_days` (recuperável, logada); `gardening` virou `tidy` (alias
   legado mantido) e sumiu do help. Stubs (`history/diff/revert/tier`) removidos.
   `memex` sem argumentos = status.
2. **Manager-first.** Prompts de propose/distill com lente de gestão: decisões
   organizacionais E técnicas, action items/compromissos (quem/quando), fatos de
   pessoas/times/sistemas, outcomes de reunião/1:1. SCHEMA idem.
3. **Sessão · workspace · projeto — uma chave por camada.**
   - sessão (1 conversa) → `raw/` (episódica), keyed por session id;
   - workspace (pasta/repo do cwd) → `now/<workspace>.md` (memória de trabalho);
   - projeto (iniciativa/área semântica) → `wiki/` (frontmatter `project:` +
     hubs). Repo git é autoritativo; sem git, o propose INFERE o projeto do
     CONTEÚDO (sessão de gestão rodada de pasta genérica cai na iniciativa
     certa); fallback: nome da pasta.

## Dívidas conscientes

- `claude /login` é o único passo manual (o CLI standalone não herda a auth do
  app desktop). Sem login, notas ficam pendentes — nada se perde.
- Cursor/Codex: sessões já ingerem; os hooks nativos deles ficam para depois.
- Gardening agendado e ADRs com cadeia de supersessão explícita: roadmap.
