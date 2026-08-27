## Regra

Alertar sobre picos de custo no Databricks via job diário: compara o custo do dia
contra a média móvel de 7 dias e dispara no canal do time quando ultrapassa 150%.

## Decisões

- **Não alertar por execução individual** — só agregado por workspace, para
  evitar ruído.
- **Média móvel de 7 dias** em vez de limite fixo (o custo varia no começo do mês).

## Responsáveis

- Alex — implementação do job, prazo: sexta-feira.
