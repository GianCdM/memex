"""Test merge models with the REAL pipeline prompt (not simplified)."""
import sys, json, time
sys.path.insert(0, '/root/src/memex')
from memex import config as config_mod
from memex.providers import _complete_openai_compat

name, kind, settings = config_mod.resolve_provider('openrouter')

# Simulate a PROPOSE output
propose_result = """{
  "skip": false,
  "slug": "lang-agnostic-pipeline",
  "section": "topics",
  "tags": ["pipeline", "redesign", "language-agnostic"],
  "title": "Pipeline redesign: lang-agnostic, 3 roles, quote-optional",
  "summary": "The synth pipeline was redesigned from 5 to 3 model roles, removed hardcoded word heuristics, made claims quote-optional, and added deterministic changelog."
}"""

# Simulate the raw excerpt that would be fed to merge
raw = open('/root/memex-test/.memex/raw/2026-08-17--claude--cab7f66b-69f5-4248-90e7-f29adf5a--abc58c0df402.md').read()
raw_excerpt = raw[40000:55000]

# Build the merge prompt EXACTLY as the pipeline does
merge_prompt = f"""You are a wiki writer. Given a session excerpt and a proposal, write or update a wiki page.

PROPOSAL:
{propose_result}

SESSION EXCERPT:
{raw_excerpt}

Write the wiki page in Markdown. Be concise, factual, and extract durable knowledge. Do NOT copy the conversation verbatim — synthesize.

Start with a brief intro paragraph, then cover the key points as needed. Use bullet lists for facts/decisions.

Your response is the page body ONLY — no frontmatter, no title."""
models = [
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "Ultra (merge anterior)"),
    ("nvidia/nemotron-3-super-120b-a12b:free", "Super-120B (merge atual)"),
    ("nvidia/nemotron-3.5-lightning:free", "Lightning"),
]

print("=" * 60)
print("REAL PIPELINE MERGE TEST (com proposta + raw excerpt)")
print("=" * 60)
print()

for model_id, label in models:
    print(f"── {label} ──")
    start = time.time()
    try:
        result = _complete_openai_compat(merge_prompt, model_id, settings, json_mode=False)
        elapsed = time.time() - start
        print(f"  Tempo: {elapsed:.1f}s | Tamanho: {len(result)} chars")
        print(f"  Conteudo:")
        print(result[:1000])
        print()
        # Quick quality check
        is_transcript = "você está" in result.lower()[:200] or "you are" in result.lower()[:200] or "absolutamente" in result.lower()[:200]
        has_intro = len(result[:200].split('\n')) > 1
        print(f"  Qualidade: {'❌ Transcript' if is_transcript else '✅'} | {'✅ Tem intro' if has_intro else '⚠️ Curto'}")
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ERRO ({elapsed:.1f}s): {e}")
    print()
    print("---")
    print()