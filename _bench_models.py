"""Benchmark free models for propose (routing) and merge (writing) quality."""
import sys, json, time
sys.path.insert(0, '/root/src/memex')
from memex import config as config_mod
from memex.providers import _complete_openai_compat

name, kind, settings = config_mod.resolve_provider('openrouter')
print(f"Provider: {name} ({kind})")

# Read a snippet from the raw note — mid-section about the pipeline redesign
raw = open('/root/memex-test/.memex/raw/2026-08-17--claude--cab7f66b-69f5-4248-90e7-f29adf5a--abc58c0df402.md').read()
snippet = raw[80000:92000]

# ── ROUND 1: PROPOSE (routing) ──────────────────────────────────
propose_models = [
    ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "Nano-Omni (atual proposta)"),
    ("nvidia/nemotron-3.5-lightning:free", "Lightning"),
    ("google/gemma-4-26b-a4b-it:free", "Gemma4-26B"),
    ("nvidia/nemotron-3-super-120b-a12b:free", "Super-120B"),
]

propose_prompt = f"""Given this AI session excerpt, decide where to file it as a wiki page.

Excerpt:
{snippet[:3000]}

Reply with JSON:
{{"skip": false, "slug": "short-kebab-slug", "section": "topics|decisions|entities|projects", "tags": ["tag1", "tag2"], "title": "Short Title", "summary": "1 sentence"}}"""

print("=" * 60)
print("ROUND 1: PROPOSE (routing quality)")
print("=" * 60)
print()

for model_id, label in propose_models:
    print(f"── {label} ──")
    start = time.time()
    try:
        result = _complete_openai_compat(propose_prompt, model_id, settings, json_mode=True)
        elapsed = time.time() - start
        data = json.loads(result)
        print(f"  Tempo: {elapsed:.1f}s")
        print(f"  Slug: {data.get('slug', 'MISSING')}")
        print(f"  Section: {data.get('section', 'MISSING')}")
        print(f"  Skip: {data.get('skip', 'MISSING')}")
        print(f"  Title: {data.get('title', 'MISSING')}")
        print(f"  Summary: {data.get('summary', 'MISSING')}")
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ERRO ({elapsed:.1f}s): {e}")
        if 'result' in dir():
            print(f"  Raw: {result[:200]}")
    print()

# ── ROUND 2: MERGE (writing quality) ────────────────────────────
merge_models = [
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "Ultra (atual merge)"),
    ("nvidia/nemotron-3-super-120b-a12b:free", "Super-120B"),
    ("nvidia/nemotron-3.5-lightning:free", "Lightning"),
    ("google/gemma-4-31b-it:free", "Gemma4-31B"),
]

merge_prompt = f"""Write a concise wiki page from this AI session excerpt.

Title: Pipeline redesign — 3 model roles
Tags: pipeline, optimization, llm, memex

Excerpt:
{snippet[:4000]}

Write a well-structured Markdown page with:
- Brief intro (2-3 sentences about the redesign)
- Key decisions made (as bullet points)
- What was removed and why

Keep it factual, <300 words."""

print("=" * 60)
print("ROUND 2: MERGE (writing quality)")
print("=" * 60)
print()

for model_id, label in merge_models:
    print(f"── {label} ──")
    start = time.time()
    try:
        result = _complete_openai_compat(merge_prompt, model_id, settings, json_mode=False)
        elapsed = time.time() - start
        print(f"  Tempo: {elapsed:.1f}s")
        print(f"  Tamanho: {len(result)} chars")
        print(f"  Conteudo:")
        print(result[:800])
        print()
        print("---")
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ERRO ({elapsed:.1f}s): {e}")
    print()