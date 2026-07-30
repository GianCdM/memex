"""memex synth — compile raw/ notes into the wiki/. The only LLM step.

Two-phase per raw note (provider-agnostic):
  1. propose (cheap model): where to file it (slug/section/tags/related) or skip.
  2. merge   (strong model): write/update the page, merging into existing content,
     with frontmatter + [[wikilinks]] + source citations + changelog.

Kinds (by source) are purely informational — no behavioral differences.
Pages carry a `status` field (current/superseded/obsolete/...) and an
auto-maintained `## 📋 Histórico` changelog section.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from . import config as config_mod
from . import limits as limits_mod
from . import providers

KIND_RANK = {"merged": 0, "session": 1, "doc": 2, "code": 3, "manual": 4}


def _summary_from(text: str) -> str:
    """Thin wrapper around analyze._extract_summary — kept here to avoid an
    import cycle at module load time (analyze.py already imports synth)."""
    from . import analyze as _analyze  # local import breaks the cycle
    return _analyze._extract_summary(text or "")

PROPOSE_PROMPT = """You organize a personal knowledge wiki — the second brain of its OWNER.
It is built from their AI sessions (which may cover management, meetings, architecture,
tech-leadership and coding), plus docs and code.

OWNER PROFILE (from the vault's ABOUT.md — judge relevance from THEIR perspective):
{about}

Given the current INDEX of pages and one RAW note, decide how to file it.

Reply with STRICT JSON only, no prose:
{{"skip": false, "slug": "kebab-case-id", "title": "Human Title", "section": "topics", "tags": ["kebab1","kebab2"], "related": ["existing-slug"], "project": "kebab-or-null", "distill": "1-3 sentences of the durable knowledge"}}

Rules:
- section is one of: topics | entities | decisions
  · entities  — people, teams, services, systems, vendors (one page per entity)
  · decisions — any decision with context/consequences, organizational OR technical
  · topics    — everything else: processes, strategies, how-tos, domain knowledge
- "project": the project/initiative/area this clearly belongs to (a repo name, an
  initiative like "okr-q3-checkout", a team's area), or null when unclear.
- PREFER REUSING an existing slug. If the note is about the same topic/feature/component as a page already in the INDEX — even from a different session, angle, or iteration — REUSE that slug so the facets merge into ONE page. Create a NEW slug ONLY for a genuinely distinct topic not covered by any existing page. When in doubt, REUSE. NEVER create near-duplicate pages for the same thing (e.g. "...-guide", "...-system-prompt", "...-protocol", "...-instructions", "...-v2" of an existing page) — those all belong in the existing page. Split only truly separate concerns (e.g. "prism-reviewer" vs "prism-storage").
- "related": REQUIRED 2-6 slugs of existing pages this connects to. A wiki without cross-links is a pile of notes, not a brain — every page should reach its neighbors. Look through the INDEX and pick: same project, shared entities/systems/people, related concepts, same domain area, adjacent decisions. Even loose thematic connections count. Return [] ONLY when the note is genuinely first-of-its-kind (rare — most notes touch something already in the brain).
- "skip": true if there is no durable knowledge worth a page (chit-chat, trivial).

INDEX (existing pages):
{index}

RAW NOTE (source={source}, kind={kind}):
{raw}
"""

# DISTILL: a noisy session transcript -> durable knowledge (heavy compression, N->1).
DISTILL_MERGE_PROMPT = """You maintain a personal knowledge wiki in Markdown (Obsidian-style).
DISTILL the RAW session into the BODY of a page: extract only the DURABLE, reusable knowledge and merge it into the existing body. Output ONLY the Markdown body — NO YAML frontmatter, NO --- fences, NO H1 title line (the tool adds those automatically). Start DIRECTLY with the content (a `## heading` or a sentence) — NO preamble or meta-commentary (e.g. "Here is...", "Based on the conversation...", "Here's the body:").

OWNER PROFILE (from the vault's ABOUT.md — "durable" means durable FOR THIS PERSON):
{about}

The lens — KEEP these, drop the rest:
- decisions + their rationale, organizational or technical ("chose X over Y because Z")
- action items & commitments: WHO committed to WHAT, by WHEN
- facts about people, teams, systems: ownership, stakeholder positions, org structure
- outcomes of meetings/1:1s/reviews worth keeping (conclusions, not minutes)
- user corrections / gotchas ("actually, do it this way")
- non-obvious solutions / workarounds that took several tries
- domain facts about the system/codebase surfaced in the conversation
Drop: chit-chat, tool-call noise, dead-ends, transient debugging, and any transcription of the log.

Rules:
- MERGE new info into the existing body; never duplicate or transcribe a chat log.
- Keep the page ON-TOPIC for its title; integrate under the right heading WITHOUT drifting scope.
- Concise and factual.
- **Cross-linking (mandatory when related pages are given)**: weave the provided [[wikilinks]] into the prose where they naturally belong (first mention of the concept, "see also" context, an inline reference). Do NOT dump them in a "Related" section at the end — integrate them in the body. Related pages to link: {related}
- Keep the content's own language (Portuguese / English as written).
- **Changelog (mandatory):** append exactly ONE line to the `## 📋 Histórico`
  section at the END of the page, summarizing what changed in this merge
  (1-2 sentences max). Format: `- \`YYYY-MM-DD\` — summary ([fonte](raw/{raw_fname}))`.
  Keep at most 10 entries (oldest first, newest last). If nothing substantive
  changed, skip. Never remove the section — if it doesn't exist, create it.

EXISTING BODY (may be empty):
{existing}

RAW SOURCE (source={source}, id={sid}):
{raw}
"""

# ADOPT: already-curated prose (README/ADR/note) -> preserve near-verbatim, just file + link.
ADOPT_MERGE_PROMPT = """You maintain a personal knowledge wiki in Markdown (Obsidian-style).
The RAW source is ALREADY curated prose (a README, ADR, or hand-written note) — it is already knowledge. ADOPT it: preserve the author's wording and structure faithfully. Output ONLY the Markdown body — NO YAML frontmatter, NO --- fences, NO H1 title line (the tool adds those automatically). Start DIRECTLY with the content — NO preamble or meta-commentary.

Rules:
- PRESERVE the prose near-verbatim. Do NOT summarize, compress, or rewrite it in your own words.
- Light touch only: fix obvious formatting, normalize heading levels (start at `##`), strip boilerplate (badges, license footers, navigation/TOC).
- If an EXISTING BODY is present, integrate the new material without dropping either side's content.
- Add [[wikilinks]] to related pages where natural: {related}
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


# placeholder echoes models produce for the "project" field (the prompt's own
# example values / literal null-as-string) — same class of junk as _TAG_JUNK
_PROJECT_JUNK = {"kebab-or-null", "null", "none", "unclear", "n-a", "project"}


def _resolve_project(cwd, prop):
    """A wiki page's project: git repo (authoritative) > content-inferred by the
    propose step > folder name. A manager's session run from a generic folder
    (home dir, notes dir) gets its project from WHAT it is about, not from
    where it happened to run — so hubs group initiatives, not directories."""
    from . import workspace as workspace_mod
    slug_from_cwd, from_git = workspace_mod.project_key_detail(cwd)
    if from_git:
        return slug_from_cwd
    proposed = prop.get("project")
    proposed = _kebab(proposed) if isinstance(proposed, str) else ""
    if proposed in _PROJECT_JUNK:
        proposed = ""
    return proposed or slug_from_cwd


def _read_about(vault, max_chars=900):
    """The owner's ABOUT.md, for prompt injection. Nothing about the owner is
    hardcoded in memex — this file IS the persona, and the user owns it."""
    try:
        text = (Path(vault) / "ABOUT.md").read_text(encoding="utf-8", errors="ignore").strip()
        return text[:max_chars] if text else "(no profile provided)"
    except OSError:
        return "(no profile provided)"


def _excerpt(body, budget):
    """What the model sees of a raw note. Long sessions carry decisions
    throughout, so take the head AND the tail instead of just the head —
    the tail is where the final state and conclusions live."""
    body = body or ""
    if len(body) <= budget:
        return body
    half = budget // 2
    return body[:half] + "\n\n[... trecho do meio omitido ...]\n\n" + body[-half:]


def _index_summary(idx):
    pages = idx.get("pages", [])
    if not pages:
        return "(empty - no pages yet)"
    return "\n".join(
        f"- {p['slug']} [{p.get('kind', 'session')}] - {p.get('title', '')}: {p.get('summary', '')[:80]}"
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


def _clean_tags(tags, max_tags=8):
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
    return out[:max_tags]


def _prune_wikilinks(body, valid_slugs):
    """Unwrap [[links]] whose target isn't a known/related page slug (kills hallucinated links)."""
    def repl(m):
        slug = re.sub(r"[^a-z0-9]+", "-", m.group(1).strip().lower()).strip("-")
        return m.group(0) if slug in valid_slugs else m.group(1)
    return re.sub(r"\[\[([^\]]+)\]\]", repl, body or "")


def _tokens(*parts):
    """Extract meaningful tokens from a page's identity: title, tags, slug, project.
    Used by the lexical fallback to score similarity between pages."""
    text = " ".join(str(p or "") for p in parts).lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if len(t) >= 3}


def _lexical_related(prop, project, existing_pages, exclude_slug, k=3):
    """Fallback linker: when the propose model returns no `related` (or too few),
    score existing pages by token overlap on {slug, title, tags, project} and
    return the top-k slugs. This is the safety net that keeps the graph
    connected even when the LLM forgets to link — a wiki without cross-links
    is a pile of notes, not a brain."""
    if not existing_pages:
        return []
    query = _tokens(prop.get("title"), prop.get("slug"), project,
                    " ".join(prop.get("tags") or []))
    if not query:
        return []
    scored = []
    for p in existing_pages:
        if p["slug"] == exclude_slug:
            continue
        target = _tokens(p.get("title"), p["slug"], p.get("project"),
                         " ".join(p.get("tags") or []))
        if not target:
            continue
        overlap = len(query & target)
        if overlap < 1:
            continue
        # boost when same project (strong signal for a link)
        same_project = 1 if project and p.get("project") == project else 0
        score = overlap + same_project * 2
        scored.append((score, p["slug"]))
    scored.sort(reverse=True)
    return [s for _, s in scored[:k]]


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


def _render_page(*, title, tags, kind, status="current", superseded_by=None,
                 sources, body, project=None):
    """Build the page: memex-owned YAML frontmatter + the model's body."""
    def yaml_list(items):
        return ("\n" + "\n".join(f"  - {i}" for i in items)) if items else " []"

    safe_title = str(title).replace('"', "'")
    fm = (
        "---\n"
        f'title: "{safe_title}"\n'
        f"tags:{yaml_list(tags)}\n"
        f"kind: {kind}\n"
        f"status: {status}\n"
        + (f"superseded_by: {superseded_by}\n" if superseded_by else "")
        + (f"project: {project}\n" if project else "")
        + f"sources:{yaml_list(sources)}\n"
        f"updated: {date.today().isoformat()}\n"
        "---\n\n"
    )
    # kind label as opening blockquote
    kind_labels = {
        "session": "> 💬 Sessão de IA\n",
        "doc": "> 📄 Documento\n",
        "manual": "> ✍️ Salvo manualmente\n",
        "code": "> 🏛️ Código\n",
        "merged": "> 🔀 Consolidado\n",
    }
    label = kind_labels.get(kind, "")
    return fm + label + "\n" + (body or "").rstrip() + "\n"


def _pid_alive(pid):
    """True if a process with this pid currently exists. Delegates to proc —
    on Windows, os.kill(pid, 0) TERMINATES the target instead of probing it."""
    from . import proc
    return proc.pid_alive(pid)


def _acquire_lock(vault):
    """Atomically claim .memex/synth.lock so two synths never run on one vault at
    once (e.g. the SessionEnd auto-synth firing while a manual synth runs). Returns
    the lock Path if we got it, or None if another LIVE synth already holds it. A
    lock owned by a dead pid (a crashed run) is treated as stale and taken over."""
    lock = vault / ".memex" / "synth.lock"
    for _ in range(2):  # one retry, to steal a stale lock
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                holder = int((lock.read_text(encoding="utf-8") or "").strip() or 0)
            except Exception:
                holder = 0
            if holder and _pid_alive(holder):
                return None  # a live synth owns it — stand down
            try:  # stale (crashed owner) → drop it and retry the claim
                lock.unlink()
            except FileNotFoundError:
                pass
    return None


def run(args) -> int:
    """Hold a per-vault lock so the SessionEnd auto-synth and a manual synth can't
    race on the same vault (they share synthed.json / index.json). Real work is in
    _run_impl; the lock is always released, even on error."""
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        return _run_impl(args)  # let _run_impl emit the proper 'not a vault' error
    lock = _acquire_lock(vault)
    if lock is None:
        print(f"another synth is already running on {vault} — skipping this run.")
        return 0
    try:
        return _run_impl(args)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _run_impl(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex vault new` first).")
        return 1

    lim = limits_mod.load(vault)
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
        synthed = json.loads(synthed_path.read_text(encoding="utf-8"))
    except Exception:
        synthed = {}

    # filename filters FIRST, hashing after — `memex remember`'s inline synth
    # of one note must not re-read the whole raw/ corpus
    if getattr(args, "only", None):  # `memex remember` compiles just its own note
        raw_files = [f for f in raw_files if f.name == args.only]
    if getattr(args, "since", None):
        raw_files = [f for f in raw_files if f.name >= args.since]
    todo = []
    for f in raw_files:
        h = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        if synthed.get(f.name) != h:
            todo.append((f, h))
    if getattr(args, "limit", None):
        todo = todo[: args.limit]

    if not todo:
        print("nothing new to synthesize.")
        return 0
    print(f"{len(todo)} raw note(s) to process.\n")

    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        idx = {"pages": []}
    pages_by_slug = {p["slug"]: p for p in idx.get("pages", [])}
    about = _read_about(vault)
    changelog = vault / ".memex" / "changelog.jsonl"
    # ── parallel synth: ThreadPoolExecutor + write lock ──────────────────
    workers = getattr(args, "workers", None) or lim.get("synth_workers", 1)
    write_lock = threading.Lock()
    _err_cnt = [0]       # mutable closure for consecutive_errors
    _errored = [0]       # mutable closure for errored
    _processed = [0]     # mutable closure for processed counter
    _stop = [False]       # circuit-breaker flag (checked by workers before LLM calls)

    # Deep-copy the index as a snapshot so ALL parallel proposes see the same
    # picture of the brain. This differs from the sequential loop (where each
    # propose sees the progressively updated index), but is safe: the propose
    # step only suggests a slug; the merge step reads the actual existing body
    # from disk (which may already include another worker's merge). Two notes
    # proposing the same new slug is fine — the second merge integrates into
    # the page the first one created.
    idx_snapshot = json.loads(json.dumps(idx))

    total = len(todo)

    def _process_one(f, h, idx_at_start):
        """Propose → merge → write (write phase serialized via write_lock)."""
        # circuit breaker check before any LLM call
        if _stop[0]:
            return None

        meta, body = _read_frontmatter(f.read_text(encoding="utf-8"))
        source, sid = meta.get("source", "doc"), meta.get("id", f.stem)
        note_kind = meta.get("kind", "session")
        raw_excerpt = _excerpt(body, lim["raw_excerpt_chars"])

        # ── phase 1: propose (parallel, readonly) ──
        try:
            p1 = providers.complete(
                PROPOSE_PROMPT.format(about=about, index=_index_summary(idx_at_start),
                                      source=source, kind=note_kind, raw=raw_excerpt),
                kind=kind, model=model_propose, settings=settings, json_mode=True)
        except Exception as e:
            with write_lock:
                _errored[0] += 1
                _err_cnt[0] += 1
                _processed[0] += 1
                print(f"  [{_processed[0]}/{total}] {f.name}: provider error: {e} — skipping (stays pending)")
                if _err_cnt[0] >= 5:
                    _stop[0] = True
            return None

        prop = _extract_json(p1) or {}

        # ── skip check ──
        if prop.get("skip"):
            with write_lock:
                _processed[0] += 1
                _err_cnt[0] = 0
                synthed[f.name] = h
                synthed_path.write_text(json.dumps(synthed, indent=2) + "\n", encoding="utf-8")
                print(f"  [{_processed[0]}/{total}] {f.name}: skipped (no durable knowledge)")
            return None

        # ── resolve slug, related, project (readonly, no lock needed) ──
        slug = (
            _kebab(prop.get("slug")) or _kebab(prop.get("title"))
            or _kebab((prop.get("distill") or "")[:50]) or f"note-{str(sid)[:8]}"
        )[:lim["slug_max"]].strip("-") or f"note-{str(sid)[:8]}"
        section = prop.get("section") if prop.get("section") in ("topics", "entities", "decisions") else "topics"
        # related slugs: only those that exist in the snapshot (the merge phase
        # will re-check under the write lock in case a page was just created)
        related = [_kebab(r) for r in (prop.get("related") or [])
                   if isinstance(r, str) and _kebab(r) in pages_by_slug]

        # LEXICAL FALLBACK: if the propose model returned no (or too few) related
        # slugs, score existing pages by token overlap and inject the top matches.
        # This keeps the graph connected even when the LLM forgets — the merge
        # step will incorporate these as [[wikilinks]] in the body.
        if len(related) < 2:
            proj_for_scoring = _resolve_project(meta.get("cwd"), prop)
            fallback = _lexical_related(
                prop, proj_for_scoring, list(pages_by_slug.values()),
                exclude_slug=slug, k=3 - len(related))
            for r in fallback:
                if r not in related:
                    related.append(r)

        # ── phase 2: merge (parallel, readonly) ──
        # Read existing body from DISK — another worker may have created/updated
        # this page since the snapshot, so we read the latest on-disk state.
        # We do NOT hold the lock here (the LLM call is expensive); a brief
        # stale read is harmless because the merge handles integration.
        existing_pre = pages_by_slug.get(slug)
        page_path_pre = (vault / "wiki" / existing_pre["path"]) if existing_pre else (vault / "wiki" / section / f"{slug}.md")
        existing_full_pre = page_path_pre.read_text(encoding="utf-8") if page_path_pre.exists() else ""
        _, existing_body_pre = _read_frontmatter(existing_full_pre)

        merge_prompt = ADOPT_MERGE_PROMPT if source == "doc" else DISTILL_MERGE_PROMPT
        merge_kwargs = dict(
            existing=existing_body_pre or "(none yet)", source=source, sid=sid,
            raw=raw_excerpt, raw_fname=f.name,
            related=", ".join(f"[[{r}]]" for r in related) or "(none)")
        if merge_prompt is DISTILL_MERGE_PROMPT:
            merge_kwargs["about"] = about
        try:
            merged_body = providers.complete(
                merge_prompt.format(**merge_kwargs),
                kind=kind, model=model_merge, settings=settings)
        except Exception as e:
            with write_lock:
                _errored[0] += 1
                _err_cnt[0] += 1
                _processed[0] += 1
                print(f"  [{_processed[0]}/{total}] {f.name}: provider error: {e} — skipping (stays pending)")
                if _err_cnt[0] >= 5:
                    _stop[0] = True
            return None

        merged_body = _clean_body(merged_body)

        # ── phase 3: write (serial, under lock) ──
        with write_lock:
            # Re-resolve the page under the lock: another worker may have
            # created or updated this slug while we were in the merge call.
            existing = pages_by_slug.get(slug)
            project = (existing.get("project") if existing else None) or _resolve_project(meta.get("cwd"), prop)
            page_path = (vault / "wiki" / existing["path"]) if existing else page_path_pre
            # Re-read the body that's actually on disk NOW (may differ from
            # what we read before the merge call if another worker wrote it).
            existing_full = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
            _, existing_body_now = _read_frontmatter(existing_full)

            # Resolve kind: the raw note's kind, or if existing page has a
            # stronger kind, keep that (manual > code > doc > session > merged)
            raw_kind = meta.get("kind", "session")
            if raw_kind not in KIND_RANK:
                raw_kind = "session"
            new_kind = raw_kind
            if existing and KIND_RANK.get(existing.get("kind", "session"), 1) <= KIND_RANK.get(raw_kind, 1):
                new_kind = existing.get("kind", "session")

            # Preserve existing status and superseded_by unless the raw note
            # explicitly signals a change
            new_status = existing.get("status", "current") if existing else "current"
            new_superseded_by = existing.get("superseded_by") if existing else None

            # Re-filter related slugs against the now-current pages_by_slug
            related_now = [r for r in related if r in pages_by_slug]
            merged_body = _prune_wikilinks(merged_body, set(pages_by_slug) | set(related_now))
            merged_body = _dedup_blocks(merged_body)

            src_ref = f"{source}:{sid}"
            sources = list(dict.fromkeys((existing.get("sources", []) if existing else []) + [src_ref]))
            tags = _clean_tags((existing.get("tags", []) if existing else []) + (prop.get("tags") or []), max_tags=lim["max_tags"])
            title = (existing.get("title") if existing else None) or prop.get("title") or slug
            page_text = _render_page(title=title, tags=tags, kind=new_kind,
                                     status=new_status, superseded_by=new_superseded_by,
                                     sources=sources, body=merged_body, project=project)

            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(page_text, encoding="utf-8")

            rel = str(page_path.relative_to(vault / "wiki"))
            pages_by_slug[slug] = {
                "slug": slug, "title": title,
                "section": (existing.get("section", section) if existing else section),
                "kind": new_kind, "status": new_status,
                "tags": tags, "sources": sources, "project": project,
                "summary": _summary_from(prop.get("distill") or (existing.get("summary") if existing else "") or ""),
                "path": rel,
            }
            with changelog.open("a", encoding="utf-8") as ch:
                ch.write(json.dumps({
                    "ts": int(time.time()), "page": slug, "kind": new_kind,
                    "status": new_status,
                    "action": "update" if existing_full else "create",
                    "source": f"{source}:{sid}", "raw": f.name}) + "\n")

            _err_cnt[0] = 0
            synthed[f.name] = h
            synthed_path.write_text(json.dumps(synthed, indent=2) + "\n", encoding="utf-8")
            idx["pages"] = list(pages_by_slug.values())
            idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
            _processed[0] += 1
            print(f"  [{_processed[0]}/{total}] {f.name} -> wiki/{rel}  [{new_kind}]")

        return f.name

    # ── dispatch ──
    if workers <= 1:
        # Sequential fallback: no thread-pool overhead, exact same behavior as
        # the old loop (each propose sees the progressively updated index).
        for f, h in todo:
            _process_one(f, h, idx)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_one, f, h, idx_snapshot): f
                       for f, h in todo}
            for future in as_completed(futures):
                future.result()  # exceptions are handled inside _process_one
                if _stop[0]:
                    # Drain already-running futures so their write-lock work
                    # completes cleanly, then cancel the rest.
                    executor.shutdown(wait=True, cancel_futures=True)
                    print("  5 provider errors in a row — provider likely down; stopping (resume later).")
                    break

    errored = _errored[0]

    _write_index_md(vault, idx)
    tail = f"  ({errored} left pending after provider errors — re-run to retry)" if errored else ""
    print(f"\n✓ synth done. {len(idx['pages'])} page(s) in the wiki.{tail}")
    try:
        from . import vault as vault_mod
        vault_mod.log_append(vault, f"synth: {len(todo)} raw note(s) processed → "
                                    f"{len(idx['pages'])} wiki page(s)")
    except Exception:
        pass
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
            lines.append(f"- [[{p['slug']}]] — {p.get('summary', '')}")
        lines.append("")
    (vault / "index.md").write_text("\n".join(lines), encoding="utf-8")
    _write_project_hubs(vault, idx)


def _write_project_hubs(vault, idx):
    """Per-project hub pages. One page per project/initiative that links its
    architecture + sessions + docs. LLM-free, regenerated from the index each
    time. Projects are semantic (initiative/area/repo) — many workspaces can
    feed one project, and one generic workspace can feed many projects."""
    from collections import defaultdict
    by_proj = defaultdict(list)
    for p in idx.get("pages", []):
        if p.get("project"):
            by_proj[p["project"]].append(p)
    if not by_proj:
        return

    def _kind(p):
        srcs = [str(s) for s in (p.get("sources") or [])]
        if any(s.startswith("analyze:") for s in srcs):
            return "arch"
        if any(s.startswith("doc:") for s in srcs):
            return "doc"
        return "session"

    hubs_dir = vault / "wiki" / "projects"
    hubs_dir.mkdir(parents=True, exist_ok=True)
    for proj, plist in sorted(by_proj.items()):
        buckets = {"arch": [], "session": [], "doc": []}
        for p in plist:
            buckets[_kind(p)].append(p)
        lines = ["---", f"title: \"{proj}\"", "kind: hub",
                 "status: current", "tags: []", "sources: []",
                 f"updated: {date.today().isoformat()}", "---", "",
                 f"# {proj}", "",
                 f"*Project hub — {len(plist)} page(s), auto-generated by memex.*", ""]
        for key, label, emoji in [("arch", "Arquitetura", "🏛️"),
                                  ("session", "Sessões", "💬"),
                                  ("doc", "Docs", "📄")]:
            bucket = sorted(buckets[key], key=lambda x: x["slug"])
            if not bucket:
                continue
            lines.append(f"## {emoji} {label}")
            for p in bucket:
                lines.append(f"- [[{p['slug']}]] — {p.get('summary') or ''}")
            lines.append("")
        (hubs_dir / f"{proj}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    idx_lines = ["---", "title: \"Projects Index\"", "kind: hub",
                 "status: current", "tags: []", "sources: []",
                 f"updated: {date.today().isoformat()}", "---", "",
                 "# Projects", "",
                 "One hub per project/initiative — each ties together sessions · docs · architecture.", ""]
    for proj in sorted(by_proj):
        idx_lines.append(f"- [[{proj}]] ({len(by_proj[proj])})")
    (hubs_dir / "_index.md").write_text("\n".join(idx_lines) + "\n", encoding="utf-8")
