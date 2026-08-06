#!/usr/bin/env bash
# Live e2e on a real machine: throwaway vault + workspace + mock LLM.
# Executes the INSTALLED hook command strings exactly as the harness would
# (Git Bash), plus one leg through cmd.exe. LLM-free (localhost mock).
#
# Run from the repo root, in Git Bash (Windows) or any POSIX shell:
#   bash tests/live_e2e.sh
#
# Requires `memex` installed (uv tool install -e .) and a python on hand.
set -u

MEMEX="${MEMEX:-$(command -v memex || echo "$HOME/.local/bin/memex.exe")}"
# a python that actually RUNS (the Windows Store stub answers `command -v python`
# but only prints an install nag — probe with a real execution)
_pick_py() {
  for c in "${PY:-}" python3 python "$APPDATA/uv/tools/memex/Scripts/python.exe"; do
    [ -n "$c" ] || continue
    if [ "$("$c" -c 'print(42)' 2>/dev/null)" = "42" ]; then echo "$c"; return; fi
  done
}
PY="$(_pick_py)"
[ -n "$PY" ] || { echo "❌ no working python found (set PY=...)"; exit 1; }
PORT="${PORT:-8765}"
SCRATCH="$(mktemp -d -t memex-live-e2e-XXXXXX)"
IS_WIN=0; case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1;; esac
_w() { if [ "$IS_WIN" = 1 ]; then cygpath -w "$1"; else echo "$1"; fi; }
_j() { echo "$1" | sed 's/\\/\\\\/g'; }  # JSON-escape backslashes

fail() { echo "❌ FAIL: $1"; kill "$MOCK_PID" 2>/dev/null; exit 1; }
ok() { echo "✔ $1"; }
cleanup() { kill "$MOCK_PID" 2>/dev/null; }
trap cleanup EXIT

mkdir -p "$SCRATCH/xdg/memex" "$SCRATCH/ws/.git"
export XDG_CONFIG_HOME="$SCRATCH/xdg"   # isolate the global config
WS="$(_w "$SCRATCH/ws")"; WS_JSON="$(_j "$WS")"
VAULT="$(_w "$SCRATCH/vault")"

"$PY" "$(dirname "$0")/mock_llm.py" "$PORT" & MOCK_PID=$!
cat > "$SCRATCH/xdg/memex/config.json" <<EOF
{"provider": {"order": ["openai_compat"],
  "openai_compat": {"base_url": "http://127.0.0.1:$PORT/v1", "api_key": null,
                    "model_propose": "mock", "model_merge": "mock"}}}
EOF

echo "== init (throwaway) =="
"$MEMEX" init --workspace "$WS" --vault "$VAULT" --no-analyze --no-docs --no-index --no-skill \
  > "$SCRATCH/init.log" 2>&1 || fail "init rc: $(tail -3 "$SCRATCH/init.log")"
grep -q "brain hooks installed" "$SCRATCH/init.log" || fail "hooks not installed"
ok "init + hooks"

get_cmd() { "$PY" -c "import json;print(json.load(open(r'''$(_w "$SCRATCH/ws")/.claude/settings.local.json'''))['hooks']['$1'][0]['hooks'][0]['command'])"; }
BOOT_CMD=$(get_cmd SessionStart)
RECALL_CMD=$(get_cmd UserPromptSubmit)
END_CMD=$(get_cmd SessionEnd)
PRECOMPACT_CMD=$(get_cmd PreCompact)

echo "== 1. SessionStart on empty brain (must be silent) =="
OUT=$(echo "{\"source\":\"startup\",\"cwd\":\"$WS_JSON\",\"session_id\":\"live-1\"}" | eval "$BOOT_CMD") || fail "boot rc"
[ -z "$OUT" ] || fail "boot not silent on empty brain: $OUT"
ok "boot silent on empty brain"

echo "== 2. fake transcript + PreCompact (partial capture) =="
T="$SCRATCH/live-sess.jsonl"
cat > "$T" <<EOF
{"type":"user","cwd":"$WS_JSON","timestamp":"2026-07-11T18:00:00Z","message":{"content":"Os dashboards de vendas mostram pedidos duplicados depois do reprocessamento."}}
{"type":"assistant","cwd":"$WS_JSON","message":{"content":[{"type":"text","text":"Vamos deduplicar no pipeline: chave order_id com janela de 24h."},{"type":"tool_use","name":"Write","input":{"file_path":"etl/dedup.sql"}}]}}
{"type":"user","cwd":"$WS_JSON","message":{"content":"Fechado, decidimos: dedup por order_id + janela de 24h. Amanhã aplicamos no job noturno."}}
EOF
T_JSON="$(_j "$(_w "$T")")"
echo "{\"transcript_path\":\"$T_JSON\",\"cwd\":\"$WS_JSON\",\"session_id\":\"live-1\"}" | eval "$PRECOMPACT_CMD" >/dev/null || fail "precompact rc"
[ "$(ls "$SCRATCH/vault/raw/" | wc -l)" -eq 1 ] || fail "partial capture raw count"
ok "PreCompact captured transcript (partial)"

echo "== 3. SessionEnd (capture + DETACHED reflect with mock LLM) =="
echo "{\"transcript_path\":\"$T_JSON\",\"cwd\":\"$WS_JSON\",\"session_id\":\"live-1\",\"reason\":\"exit\"}" | eval "$END_CMD" > "$SCRATCH/end.log" 2>&1 || fail "sessionend rc"
grep -q "reflect spawned" "$SCRATCH/end.log" || fail "reflect not spawned: $(cat "$SCRATCH/end.log")"
PROJ=$("$PY" -c "from memex.workspace import workspace_key; print(workspace_key(r'''$WS'''))")
for i in $(seq 1 30); do [ -f "$SCRATCH/vault/workspace/$PROJ.md" ] && break; sleep 1; done
[ -f "$SCRATCH/vault/workspace/$PROJ.md" ] || fail "detached reflect never wrote workspace/$PROJ.md"
grep -q "job noturno" "$SCRATCH/vault/workspace/$PROJ.md" || fail "workspace-page content wrong"
ls "$SCRATCH/vault/wiki/topics/" | grep -q "pipeline-vendas-dedup" || fail "wiki page missing"
ok "SessionEnd -> detached reflect -> wiki page + workspace-page"

echo "== 4. NEW session boots with working memory =="
OUT=$(echo "{\"source\":\"startup\",\"cwd\":\"$WS_JSON\",\"session_id\":\"live-2\"}" | eval "$BOOT_CMD") || fail "boot2 rc"
echo "$OUT" | grep -q "Where we left off" || fail "boot missing workspace-page"
echo "$OUT" | grep -q "job noturno" || fail "boot missing next steps"
ok "boot injects 'where we left off' in the NEW session"

echo "== 5. recall in the new session (+ dedup) =="
P="{\"session_id\":\"live-2\",\"prompt\":\"como ficou a deduplicacao de pedidos no pipeline de vendas?\"}"
OUT=$(echo "$P" | eval "$RECALL_CMD") || fail "recall rc"
echo "$OUT" | grep -q "pipeline-vendas-dedup" || fail "recall missed page: $OUT"
OUT2=$(echo "$P" | eval "$RECALL_CMD") || fail "recall2 rc"
[ -z "$OUT2" ] || fail "recall did not dedup within session"
ok "recall injects page with path; dedups within session"

echo "== 6. deliberate handoff wins over auto =="
(cd "$SCRATCH/ws" && printf '## Contexto\nHandoff manual do agente.\n## Próximos passos\n- [ ] revisar PR\n' | "$MEMEX" handoff --stdin --vault "$VAULT") >/dev/null || fail "handoff rc"
"$MEMEX" reflect --vault "$VAULT" --cwd "$WS" >/dev/null 2>&1
grep -q "Handoff manual" "$SCRATCH/vault/workspace/$PROJ.md" || fail "reflect clobbered fresh handoff"
ok "fresh handoff survives reflect (hold)"

echo "== 7. remember -> instant wiki page =="
(cd "$SCRATCH/ws" && "$MEMEX" remember --vault "$VAULT" "O time decidiu usar janela de 24h como padrao de dedup em todos os pipelines.") > "$SCRATCH/rem.log" 2>&1 || fail "remember rc"
grep -q "saved" "$SCRATCH/rem.log" || fail "remember not saved"
ok "remember filed + synthesized inline"

echo "== 8. UTF-8 survives the stdin roundtrip (no mojibake) =="
(cd "$SCRATCH/ws" && printf '## Contexto\nAcentuação préservada: ação, décision.\n' | "$MEMEX" handoff --stdin --vault "$VAULT") >/dev/null || fail "handoff utf8 rc"
grep -q "Acentuação préservada" "$SCRATCH/vault/workspace/$PROJ.md" || fail "mojibake in workspace-page"
ok "UTF-8 clean end-to-end"

if [ "$IS_WIN" = 1 ]; then
  echo "== 9. same boot command through cmd.exe (quoting check) =="
  printf '{"source":"startup","cwd":"%s","session_id":"live-3"}' "$WS_JSON" > "$SCRATCH/payload.json"
  printf '@echo off\r\n%s < %s\r\n' "$BOOT_CMD" "$(cygpath -w "$SCRATCH/payload.json")" > "$SCRATCH/run_boot.bat"
  OUT=$(cmd.exe //c "$(cygpath -w "$SCRATCH/run_boot.bat")")
  echo "$OUT" | grep -q "Where we left off" || fail "cmd.exe boot failed"
  ok "hook command works under cmd.exe too"
fi

echo ""
echo "✅ LIVE E2E: all legs passed.  (scratch: $SCRATCH)"
