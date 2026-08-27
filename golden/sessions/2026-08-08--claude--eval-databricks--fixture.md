---
source: claude
id: eval-databricks
date: 2026-08-08
cwd: /tmp/eval
kind: session
---

# Sessão de exemplo — alertas de custo Databricks

Decidimos alertar sobre picos de custo no Databricks via um job diário.
O job compara o custo do dia contra a média móvel de 7 dias; se ultrapassar
150%, dispara no canal do time. O responsável por implementar é o Alex, prazo
até sexta.

Ficou acordado que não vamos alertar por execução individual — só agregado por
workspace, para não gerar ruído. Preferimos média móvel em vez de um limite
fixo porque o custo varia muito no começo do mês.
