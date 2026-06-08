"""memex synth — compile raw/ notes into the wiki/. The only LLM step.

Two-phase per raw note (provider-agnostic):
  1. propose (cheap model): where to file it (slug/section/tags/related) or skip.
  2. merge   (strong model): write/update the page, merging into existing content,
     with frontmatter + [[wikilinks]] + source citations.

Tiers (by source) govern edit behavior: gold pages snapshot the previous version
to .memex/history/ before overwriting (auditable + revertable). All edits append
to .memex/changelog.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path

from . import config as config_mod
from . import providers

TIER_RANK = {"bronze": 0, "silver": 1, "gold": 2}

PROPOSE_PROMPT = """You organize a personal knowledge wiki built from AI coding sessions, code and notes.
Given the current INDEX of pages and one RAW note, decide how to file it.

Reply with STRICT JSON only, no prose:
{{"skip": false, "slug": "kebab-case-id", "title": "Human Title", "section": "topics", "tags": ["kebab1","kebab2"], "related": ["existing-slug"], "distill": "1-3 sentences of the durable knowledge"}}

Rules:
- section is one of: topics | entities | decisions
- PREFER REUSING an existing slug. If the note is about the same topic/feature/component as a page already in the INDEX — even from a different session, angle, or iteration — REUSE that slug so the facets merge into ONE page. Create a NEW slug ONLY for a genuinely distinct topic not covered by any existing page. When in doubt, REUSE. NEVER create near-duplicate pages for the same thing (e.g. "...-guide", "...-system-prompt", "...-protocol", "...-instructions", "...-v2" of an existing page) — those all belong in the existing page. Split only truly separate concerns (e.g. "prism-reviewer" vs "prism-storage").
- "related": slugs of existing pages this should link to (may be empty).
- "skip": true if there is no durable knowledge worth a page (chit-chat, trivial).

INDEX (existing pages):
{index}

RAW NOTE (source={source}, tier={tier}):
{raw}
"""

MERGE_PROMPT = """You maintain a personal knowledge wiki in Markdown (Obsidian-style).
Write or UPDATE the BODY of a page from the RAW source. Output ONLY the Markdown body — NO YAML frontmatter, NO --- fences, NO H1 title line (the tool adds those automatically). Start DIRECTLY with the content (a `## heading` or a sentence) — NO preamble or meta-commentary (e.g. "Here is...", "Based on the conversation...", "Here's the body:"), NO leading separator line.

Rules:
- MERGE new info from the RAW source into the existing body; never duplicate or transcribe a chat log.
- Keep the page ON-TOPIC for its title; integrate new info under the right heading WITHOUT letting it hijack or drift the page's scope.
- Concise, factual, DURABLE knowledge (decisions, how & why, facts).
- Link related pages with [[wikilinks]]: {related}
- Keep the content's own language (Portuguese / English as written).

EXISTING BODY (may be empty):
{existing}

RAW SOURCE (source={source}, id={sid}):
{raw}
"""


def _read_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = {}
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def _extract_json(s):
    s = re.sub(r"```(?:json)?", "", s or "").strip()
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


def _kebab(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _index_summary(idx):
    pages = idx.get("pages", [])
    if not pages:
        return "(empty - no pages yet)"
    return "\n".join(
        f"- {p['slug']} [{p.get('tier', 'silver')}] - {p.get('title', '')}: {p.get('summary', '')[:80]}"
        for p in pages
    )


def _strip_fences(md):
    return re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", md.strip())


def _strip_frontmatter(md):
    """Drop a leading --- ... --- block if the model added one anyway."""
    md = (md or "").lstrip()
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4:].lstrip("\n")
    return md


_PREAMBLE_RE = re.compile(
    r"^\s*(here(?:'s| is| are| follows)?|sure|okay|below|the following|"
    r"this (?:is|looks|appears|seems)|based on|looking at|i'?ll|let me)\b.*:\s*$",
    re.IGNORECASE,
)
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")


def _clean_body(md):
    """Strip fences, any stray frontmatter, a conversational preamble line, and
    leading horizontal-rule/blank lines the model may prepend."""
    lines = _strip_frontmatter(_strip_fences(md)).splitlines()
    i = 0
    # drop leading blank lines, horizontal rules, and conversational preamble
    # lines (e.g. "Based on the conversation... Here's the body:") the model
    # may prepend before the real content
    while i < len(lines) and (
        not lines[i].strip()
        or _HR_RE.match(lines[i])
        or _PREAMBLE_RE.match(lines[i])
    ):
        i += 1
    return "\n".join(lines[i:]).strip()


_TAG_JUNK = {
    "a", "b", "kebab1", "kebab2", "tag", "tags", "topic", "area",
    "example", "human-title", "kebab-case-id", "existing-slug",
}


def _clean_tags(tags):
    """Normalize to kebab-case; drop placeholders the model copies from the prompt example."""
    seen, out = set(), []
    for t in tags or []:
        if not isinstance(t, str):
            continue
        t = re.sub(r"[^a-z0-9]+", "-", t.strip().lower()).strip("-")
        if len(t) < 2 or t in _TAG_JUNK or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:8]


def _prune_wikilinks(body, valid_slugs):
    """Unwrap [[links]] whose target isn't a known/related page slug (kills hallucinated links)."""
    def repl(m):
        slug = re.sub(r"[^a-z0-9]+", "-", m.group(1).strip().lower()).strip("-")
        return m.group(0) if slug in valid_slugs else m.group(1)
    return re.sub(r"\[\[([^\]]+)\]\]", repl, body or "")


def _dedup_blocks(body):
    """Drop exact-duplicate substantial paragraphs (kills model looping / copy-paste)."""
    seen, out = set(), []
    for block in re.split(r"\n\s*\n", body or ""):
        key = block.strip()
        if len(key) > 40 and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(block)
    return "\n\n".join(out)


def _render_page(*, title, tags, tier, sources, body):
    """Build the page: memex-owned YAML frontmatter + the model's body."""
    def yaml_list(items):
        return ("\n" + "\n".join(f"  - {i}" for i in items)) if items else " []"

    safe_title = str(title).replace('"', "'")
    fm = (
        "---\n"
        f'title: "{safe_title}"\n'
        f"tags:{yaml_list(tags)}\n"
        f"tier: {tier}\n"
        f"sources:{yaml_list(sources)}\n"
        f"updated: {date.today().isoformat()}\n"
        "---\n\n"
    )
    return fm + (body or "").rstrip() + "\n"


def run(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex vault new` first).")
        return 1

    vcfg = config_mod.load_vault(vault)
    name, kind, settings = config_mod.resolve_provider(
        getattr(args, "provider", None), vault_cfg=vcfg
    )
    model_propose = getattr(args, "model_propose", None) or settings.get("model_propose")
    model_merge = getattr(args, "model_merge", None) or settings.get("model_merge")
    if not model_propose or not model_merge:
        print(f"error: no models set for provider '{name}'. "
              "Configure them (memex config / --model-merge) or run `memex doctor`.")
        return 1

    print(f"synth: provider={name} ({kind})  propose={model_propose}  merge={model_merge}")

    raw_files = sorted((vault / "raw").glob("*.md"))
    synthed_path = vault / ".memex" / "synthed.json"
    try:
        synthed = json.loads(synthed_path.read_text())
    except Exception:
        synthed = {}

    todo = []
    for f in raw_files:
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        if synthed.get(f.name) != h:
            todo.append((f, h))
    if getattr(args, "since", None):
        todo = [(f, h) for (f, h) in todo if f.name >= args.since]
    if getattr(args, "limit", None):
        todo = todo[: args.limit]

    if not todo:
        print("nothing new to synthesize.")
        return 0
    print(f"{len(todo)} raw note(s) to process.\n")

    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text())
    except Exception:
        idx = {"pages": []}
    pages_by_slug = {p["slug"]: p for p in idx.get("pages", [])}
    changelog = vault / ".memex" / "changelog.jsonl"

    for n, (f, h) in enumerate(todo, 1):
        meta, body = _read_frontmatter(f.read_text())
        source, sid = meta.get("source", "doc"), meta.get("id", f.stem)
        tier = meta.get("tier", "silver")
        raw_excerpt = body[:6000]

        try:
            p1 = providers.complete(
                PROPOSE_PROMPT.format(index=_index_summary(idx), source=source, tier=tier, raw=raw_excerpt),
                kind=kind, model=model_propose, settings=settings, json_mode=True)
        except providers.ProviderError as e:
            print(f"  [{n}/{len(todo)}] {f.name}: provider error: {e}")
            return 2

        prop = _extract_json(p1) or {}
        if prop.get("skip"):
            print(f"  [{n}/{len(todo)}] {f.name}: skipped (no durable knowledge)")
            synthed[f.name] = h
            synthed_path.write_text(json.dumps(synthed, indent=2) + "\n")
            continue

        slug = (
            _kebab(prop.get("slug")) or _kebab(prop.get("title"))
            or _kebab((prop.get("distill") or "")[:50]) or f"note-{str(sid)[:8]}"
        )[:60].strip("-") or f"note-{str(sid)[:8]}"
        section = prop.get("section") if prop.get("section") in ("topics", "entities", "decisions") else "topics"
        related = [r for r in (prop.get("related") or []) if isinstance(r, str)]

        existing = pages_by_slug.get(slug)
        page_path = (vault / "wiki" / existing["path"]) if existing else (vault / "wiki" / section / f"{slug}.md")
        existing_full = page_path.read_text() if page_path.exists() else ""
        _, existing_body = _read_frontmatter(existing_full)

        new_tier = tier
        if existing and TIER_RANK.get(existing.get("tier", "silver"), 1) >= TIER_RANK.get(tier, 1):
            new_tier = existing.get("tier", "silver")

        # phase 2: the model writes the BODY only; memex owns the frontmatter
        try:
            body = providers.complete(
                MERGE_PROMPT.format(
                    existing=existing_body or "(none yet)", source=source, sid=sid,
                    raw=raw_excerpt,
                    related=", ".join(f"[[{r}]]" for r in related) or "(none)"),
                kind=kind, model=model_merge, settings=settings)
        except providers.ProviderError as e:
            print(f"  [{n}/{len(todo)}] {f.name}: provider error: {e}")
            return 2
        body = _clean_body(body)
        body = _prune_wikilinks(body, set(pages_by_slug) | set(related))
        body = _dedup_blocks(body)

        # memex builds the structured frontmatter (never trusts the model for it)
        src_ref = f"{source}:{sid}"
        sources = list(dict.fromkeys((existing.get("sources", []) if existing else []) + [src_ref]))
        tags = _clean_tags((existing.get("tags", []) if existing else []) + (prop.get("tags") or []))
        title = (existing.get("title") if existing else None) or prop.get("title") or slug
        page_text = _render_page(title=title, tags=tags, tier=new_tier, sources=sources, body=body)

        # gold: snapshot previous version before overwriting (audit / revert)
        if existing_full and new_tier == "gold":
            hist = vault / ".memex" / "history" / slug
            hist.mkdir(parents=True, exist_ok=True)
            (hist / f"{int(time.time())}.md").write_text(existing_full)

        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(page_text)

        rel = str(page_path.relative_to(vault / "wiki"))
        pages_by_slug[slug] = {
            "slug": slug, "title": title,
            "section": (existing.get("section", section) if existing else section),
            "tier": new_tier, "tags": tags, "sources": sources,
            "summary": ((prop.get("distill") or (existing.get("summary") if existing else "") or ""))[:200],
            "path": rel,
        }
        with changelog.open("a") as ch:
            ch.write(json.dumps({
                "ts": int(time.time()), "page": slug, "tier": new_tier,
                "action": "update" if existing_full else "create",
                "source": f"{source}:{sid}", "raw": f.name}) + "\n")

        synthed[f.name] = h
        synthed_path.write_text(json.dumps(synthed, indent=2) + "\n")
        idx["pages"] = list(pages_by_slug.values())
        idx_path.write_text(json.dumps(idx, indent=2) + "\n")
        print(f"  [{n}/{len(todo)}] {f.name} -> wiki/{rel}  [{new_tier}]")

    _write_index_md(vault, idx)
    print(f"\n✓ synth done. {len(idx['pages'])} page(s) in the wiki.")
    # automatic, non-destructive: surface near-duplicate clusters as a gentle
    # suggestion note in the wiki (the user merges in Obsidian, or ignores it).
    try:
        from . import gardening
        n_sug = gardening.write_suggestions(vault)
        if n_sug:
            print(f"  {n_sug} organization suggestion(s) -> wiki/{gardening.SUGGESTIONS_FILE}")
    except Exception:
        pass
    return 0


def _write_index_md(vault, idx):
    sections = {"topics": [], "entities": [], "decisions": []}
    for p in idx.get("pages", []):
        sections.setdefault(p.get("section", "topics"), []).append(p)
    lines = ["# Brain index", "", "Navigable catalog of wiki pages.", ""]
    for sec, title in [("topics", "Topics"), ("entities", "Entities"), ("decisions", "Decisions")]:
        lines.append(f"## {title}")
        for p in sorted(sections.get(sec, []), key=lambda x: x["slug"]):
            lines.append(f"- [[{p['slug']}]] — {p.get('summary', '')[:100]}")
        lines.append("")
    (vault / "index.md").write_text("\n".join(lines))
