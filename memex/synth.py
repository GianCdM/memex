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
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from . import canon as canon_mod
from . import changes as changes_mod
from . import config as config_mod
from . import contracts as ctr
from . import limits as limits_mod
from . import providers
from . import verify as verify_mod
from .format import read_frontmatter as _read_frontmatter  # re-export: helpers moved to format.py

KIND_RANK = {"merged": 0, "session": 1, "doc": 2, "code": 3, "manual": 4}

# The memex capture hook sometimes ingests the memex's OWN synthesis workers:
# when a `claude -p` propose/merge/workspace/tidy process runs, its SessionEnd
# hook snapshots the worker's transcript (the system prompt + the JSON reply)
# as if it were a user session. Those raw captures are pipeline feedback, not
# durable knowledge — the source session/document they processed exists as its
# own raw file, so skipping them loses nothing.
#
# Detection is STRUCTURAL, not string-matched: a worker capture is a session
# (`source` in the session set) whose cwd resolves to the OS temp dir — the
# runner spawns `claude -p` from a temp cwd, never a real project path. This
# catches every worker (propose/merge/doc-ADOPT/tidy/workspace) without
# enumerating prompts, and cannot false-positive on real user sessions (they
# run from project dirs) or on `kind: doc` captures the ingest copied into
# temp (they are not session sources).
_SESSION_SOURCES = {"claude"}
# Generic OS tempdir shapes (macOS: /var/folders/…/T resolves to
# /private/var/folders/…/T; Linux/Unix: /tmp). Compared via realpath below,
# and also matched as prefixes so a captured runner cwd nests under them.
_TMP_PATTERNS = ("/var/folders/", "/private/tmp", "/tmp")


def _is_pipeline_artifact(meta: dict, body: str) -> bool:
    """True when a raw note is a capture of the memex's own synthesis worker.

    A worker runs `claude -p` from the OS temp dir, so the capture has a
    session source AND a temp cwd. Requiring both is conservative: a real user
    session (project cwd) or a doc capture (doc source, even under temp) is
    never skipped. If the vault runs with a non-standard TMPDIR, the runtime
    temp dir is resolved and compared too.
    """
    if not body:
        return False
    if str((meta or {}).get("source") or "").lower() not in _SESSION_SOURCES:
        return False
    cwd = str((meta or {}).get("cwd") or "")
    if not cwd:
        return False
    rp = os.path.realpath(cwd)
    # The memex runner spawns `claude -p` with cwd == the OS temp dir ITSELF
    # (e.g. /private/var/folders/…/T), never a subdirectory of it. A real user
    # session — or a test fixture — may run from a temp SUBDIR (…/T/foo), so
    # requiring cwd to BE the tempdir (not merely nest under it) avoids
    # swallowing legitimate captures. `/tmp` and `/private/tmp` are treated as
    # exact roots too.
    td = os.path.realpath(tempfile.gettempdir())
    if rp == td:
        return True
    return any(rp == p for p in _TMP_PATTERNS)


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
{{"skip": false, "slug": "kebab-case-id", "title": "Human Title", "section": "topics", "tags": ["kebab1","kebab2"], "related": ["existing-slug"], "project": "kebab-or-null", "distill": "1-3 sentences of the durable knowledge", "claims": [{{"text": "one narrow durable statement", "type": "process|fact|decision|entity|commitment", "explicitness": "explicit|inferred", "evidence": [{{"start_line": 1, "end_line": 1, "quote": "exact text copied from RAW NOTE"}}]}}]}}

Rules:
- section is one of: topics | entities | decisions
  · entities  — people, teams, services, systems, vendors (one page per entity)
  · decisions — any decision with context/consequences, organizational OR technical
  · topics    — everything else: processes, strategies, how-tos, domain knowledge
- "project": the project/initiative/area this clearly belongs to (a repo name, an
  initiative like "okr-q3-checkout", a team's area), or null when unclear.
- PREFER REUSING an existing slug. If the note is about the same topic/feature/component as a page already in the INDEX — even from a different session, angle, or iteration — REUSE that slug so the facets merge into ONE page. Create a NEW slug ONLY for a genuinely distinct topic not covered by any existing page. When in doubt, REUSE. NEVER create near-duplicate pages for the same thing (e.g. "...-guide", "...-system-prompt", "...-protocol", "...-instructions", "...-v2" of an existing page) — those all belong in the existing page. Split only truly separate concerns (e.g. "prism-reviewer" vs "prism-storage").
- "related": links of existing pages this connects to. Return only links that are explicitly relevant to the source; zero links is allowed.
- Claims MAY carry a verbatim `quote` — attach a quote only when you can copy a literal
  substring of the RAW NOTE. Never invent, paraphrase, or summarize a quote. A claim without
  a quote is still accepted (unanchored, not unsupported) — the body-fidelity check decides.
- If you cannot name a semantic title and slug, return "skip": true with no fallback identity.
- New entities and decisions are valid proposals but will be reviewed; do not downgrade them to topics.
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
- The changelog (`## Histórico`) is appended DETERMINISTICALLY after the
  merge — do NOT add or modify it. Focus only on the durable knowledge.

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
- NEVER add durable content the source does not contain: no new facts, numbers, options, profiles, thresholds, or whole sections of substantive material. The source is already the knowledge — you re-file it, you do not enrich it.
- NEVER ADD any header/footer/date/preparation line ("Prepared on", "Documento preparado para discussão", a date, your name, etc.) that is NOT present verbatim in the source. The page must contain ONLY what the source contains, re-filed. Adding a footer/date is inventing content.
- If an EXISTING BODY is present, integrate the new material without dropping either side's content.
- Add [[wikilinks]] to related pages where natural: {related} — links are navigation, not content.
- Keep the content's own language (Portuguese / English as written).

EXISTING BODY (may be empty):
{existing}

RAW SOURCE (source={source}, id={sid}):
{raw}
"""


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


def _doc_parts(body: str) -> tuple[str | None, str, list[str]]:
    """Deterministically split a raw doc body into (title, clean_body, tags).

    A captured doc is already-curated prose. Its body may carry an Obsidian-
    style YAML frontmatter block (title/tags/…) before the content; the ADOPT
    merge path preserves that, but the deterministic route derives everything
    itself. Title = the leading H1 (fallback: the frontmatter `title:`), tags =
    the frontmatter `tags:` list, clean_body = the body minus its frontmatter
    (and minus a duplicate H1 if the title came from frontmatter).
    """
    text = (body or "").lstrip("\n")
    title = tags = None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            text = text[end + 4:].lstrip("\n")
            for line in fm.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                if key == "title":
                    title = value.strip().strip("\"'")
                elif key == "name" and title is None:
                    # SKILL.md convention (Claude Code) uses `name:`/`description:`
                    # instead of `title:` — fall back to `name` so skill docs get
                    # a real slug instead of the path-hash `skill-<hash>`.
                    title = value.strip().strip("\"'")
                elif key == "tags":
                    raw = value.strip()
                    if raw.startswith("["):
                        tags = [t.strip().strip("\"'") for t in raw.strip("[]").split(",") if t.strip()]
                    elif raw:
                        tags = [raw.strip().strip("\"'")]
    # H1 wins over frontmatter title (the body is authoritative content).
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    if not title:
        return None, text, tags or []
    # If the title is an H1 already at the top, keep it in the body (it is the
    # content's own heading, not a duplicate of the page title).
    return title, text, tags or []


def _doc_slug(doc_id: str, cwd: str, slug_max: int = 60) -> str:
    """A STABLE, collision-safe slug for a captured doc, derived from its source
    path — never from an LLM. The raw path id (full source path) is kebabized
    (relative to the ingest cwd when it nests under it, else the bare stem) and
    suffixed with a short hash of the full id so two different files that share
    a stem (SKILL.md in 113 repos, material-educacional.md in 3) never collide.
    Stable across re-captures because the path id is the same."""
    raw = str(doc_id or "").strip()
    base = None
    if raw:
        if cwd and raw.startswith(str(cwd)):
            rel = os.path.relpath(raw, str(cwd))
        else:
            rel = os.path.basename(raw)
        base = _kebab(os.path.splitext(rel)[0]) or None
    if not base:
        return ""
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    keep = max(4, int(slug_max) - len(suffix) - 1)
    return f"{base[:keep].strip('-')}-{suffix}"[:slug_max].strip("-")


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


def _index_summary(idx, prompt="", limit=0):
    """The wiki index the proposer sees, BOUNDED.

    Sending the full index (hundreds of pages) on every propose call is the
    single biggest prompt-size driver — and it doesn't help the model merge a
    specific raw note, it dilutes attention on the neighbors that matter. When
    `limit > 0`, rank pages against the raw note's text with the same lexical
    scorer recall uses and show only the top `limit` related pages, so the
    model still sees the relevant neighbors (for wikilinks and dedupe) without
    the whole catalog. `limit == 0` keeps the legacy full-index behavior.
    """
    pages = idx.get("pages", [])
    if not pages:
        return "(empty - no pages yet)"
    if limit and prompt:
        try:
            from . import recall as recall_mod
            scored = recall_mod.rank(pages, prompt, {"retrieve_min_overlap": 1,
                                                     "retrieve_min_score": 0.0})
            top = [p for _, p in scored[:limit]]
        except Exception:
            top = pages[:limit]
    else:
        top = pages[:limit] if limit else pages
    if limit and len(pages) > limit:
        head = "\n".join(
            f"- {p['slug']} [{p.get('kind', 'session')}] - {p.get('title', '')}: {p.get('summary', '')[:80]}"
            for p in top)
        return head + f"\n... ({len(pages) - limit} other pages not shown)"
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
    Used by relink to score similarity between pages."""
    text = " ".join(str(p or "") for p in parts).lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if len(t) >= 3}


def _resolve_anchor(lines, quote, raw_path):
    """Resolve a claim quote to an absolute raw anchor.

    Finds the first line (1-based) in the FULL raw file (frontmatter included —
    `verify.validate_evidence` indexes the whole file) whose text contains the
    quote. Returns the anchor dict the promoter's evidence gate understands, or
    None when the quote cannot be located (the claim is ungrounded).
    """
    q = (quote or "").strip()
    if not q:
        return None
    for i, line in enumerate(lines):
        if q in line:
            return {
                "raw": f"raw/{raw_path.name}",  # legacy prefix; canon.raw_rel resolves
                "raw_sha256": canon_mod.file_hash(raw_path),
                "start_line": i + 1,
                "end_line": i + 1,
                "quote": q,
            }
    return None


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


# ─────────────────────────────────────────────────────────────────────────── #
# Deterministic triage — run BEFORE any LLM call. The plan's "don't pay 3 LLM
# calls to discover a no-op": collapse same-id snapshots, drop config-skipped
# sources, and turn append-only re-captures into delta merges.
# ─────────────────────────────────────────────────────────────────────────── #
_LINEAGE_REL = Path(".memex") / "lineage.json"


def _load_lineage(vault):
    try:
        return json.loads((vault / _LINEAGE_REL).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_lineage(vault, lineage):
    _atomic_write(vault / _LINEAGE_REL,
                  json.dumps(lineage, ensure_ascii=False, indent=2) + "\n")


def _atomic_write(path, text):
    """Write via a sibling .tmp + replace so a crash mid-flush can't corrupt."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _flush_state(vault, synthed, synthed_path, lineage, idx, metrics):
    """One-shot persistence of a synth run's batched state, in a safe order:
    synthed first (the raw-done marks; `synthed_path=None` skips it when nothing
    was marked), then lineage, then the machine-owned views, then metrics. Every
    write is atomic; a crash anywhere leaves the disk consistent with a re-run."""
    try:
        if synthed_path is not None:
            _atomic_write(synthed_path, json.dumps(synthed, indent=2) + "\n")
        _save_lineage(vault, lineage)
    except Exception:
        pass
    try:
        from . import views as views_mod
        views_mod.write_views(vault, idx)
    except Exception:
        pass
    try:
        from . import metrics as metrics_mod
        for _ev in metrics:
            metrics_mod.log(vault, _ev)
    except Exception:
        pass


# Names whose synthed/lineage flush failed this run. `_mark_done` prints a LOUD
# warning to stderr on a failed write AND records the name here, so the run's
# closing "✓ synth done" can't hide a lost mark (ENOSPC / permission). Reset at
# the start of each run (see `_run_impl`).
_failed_flushes: list[str] = []


def _mark_done(vault, synthed, synthed_path, lineage, name, h):
    """Mark a raw as processed AND flush synthed.json + lineage to disk
    immediately. Called under write_lock (or in the single-threaded post-dispatch
    pass). A kill/crash anywhere after this point preserves the mark — this is
    the loop-proof guarantee (M1): before, marks were only flushed once at
    end-of-run, so a dead reflect dropped every in-memory mark while ChangeSets
    already on disk made the next reflect reprocess the same raws.

    A write failure (ENOSPC / permissions) does NOT crash the run — the in-memory
    mark is kept and the next reflect would reprocess — but it MUST be visible:
    we print a stderr warning and record the name in `_failed_flushes` so the run
    reports it. Silently dropping the mark is what this guards against."""
    synthed[name] = h
    try:
        _atomic_write(synthed_path, json.dumps(synthed, indent=2) + "\n")
    except Exception as e:
        print(f"WARNING: synthed flush failed for {name}: {e}", file=sys.stderr)
        _failed_flushes.append(name)
    try:
        _save_lineage(vault, lineage)
    except Exception as e:
        print(f"WARNING: lineage flush failed for {name}: {e}", file=sys.stderr)
        _failed_flushes.append(name)


def _advance_delta_cursor(lineage, *, sid, cursor, raw_body=""):
    """Hands-free (auto_review): advance a session-delta's lineage CURSOR past
    content that was durably handled but NOT applied (rejected/dedup-skipped).

    In auto_review every outcome is terminal — applied OR rejected — so a delta
    that was discarded is still "seen" and must not be re-proposed on the next
    re-capture. Without this the cursor stays at the last applied checkpoint and
    the rejected tail is reprocessed forever (the loop that kept 27 chunks alive).

    We only bump the `chars` field of an EXISTING lineage entry — the page
    (slug/section/page_body_hash) is untouched. A reject has no new page, so we
    never create a fresh entry (that would delta-merge into a headless slug).
    Returns True if the cursor advanced, False otherwise."""
    prev = lineage.get(sid)
    if not prev or not prev.get("slug"):
        return False
    try:
        cursor = int(cursor)
    except (TypeError, ValueError):
        return False
    if cursor <= _checkpoint_chars(prev):
        return False
    prev["chars"] = cursor
    if raw_body:
        prev["body_hash"] = _body_hash(raw_body[:cursor])
    return True


def _attempts_path(vault) -> Path:
    return Path(vault) / ".memex" / "attempts.json"


def _load_attempts(vault) -> dict:
    try:
        return json.loads(_attempts_path(vault).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_attempts(vault, attempts) -> None:
    _atomic_write(_attempts_path(vault), json.dumps(attempts, indent=2) + "\n")


def _record_attempt(vault, attempts, name) -> int:
    attempts[name] = attempts.get(name, 0) + 1
    _save_attempts(vault, attempts)
    return attempts[name]


def _clear_attempt(vault, attempts, name) -> None:
    if name in attempts:
        del attempts[name]
        _save_attempts(vault, attempts)


def _park_raw(vault, synthed, synthed_path, lineage, attempts, name, h,
              *, raw_path, reason="", is_chunk=False,
              record_chunk_done=None, attempt_key=None) -> None:
    """M3: a raw that hit the provider-error cap is parked — marked done so it
    never reprocesses, with a `park` ChangeSet so it's visible in review.

    A CHUNK slice that parks must NOT mark the parent raw done: a sibling slice
    may still hold unprocessed content, and `_mark_done(parent)` would drop it
    from the backlog forever. Instead we feed `_chunk_done_map` (via the
    `_record_chunk_done` closure from `_process_one`) — the post-dispatch
    `finally` pass then marks the parent only when ALL its slices are handled
    (parked slices count as handled)."""
    import hashlib as _h
    from . import changes as changes_mod
    if is_chunk:
        # Parked slices count as handled: the finally pass over _chunk_done_map
        # marks the parent done only once every slice is accounted for.
        if record_chunk_done is not None:
            record_chunk_done()
    else:
        _mark_done(vault, synthed, synthed_path, lineage, name, h)
    attempts.pop(attempt_key or name, None)
    _save_attempts(vault, attempts)
    try:
        ch = changes_mod.new_changeset(
            operation="park",
            classification={"section": "topics", "slug": None, "title": None,
                            "project": None},
            source={"raw": f"raw/{name}", "raw_sha256": _h.sha256(
                Path(raw_path).read_bytes()).hexdigest(), "kind": "raw", "mode": "park"},
            target={"slug": None},
            claims=[],
            proposed_body="",
            risk="park",
            reason=reason or "parked after repeated provider errors")
        changes_mod.save_changeset(vault, ch)
    except Exception:
        pass


def _normalize_ws(text: str) -> str:
    """Collapse whitespace for a containment / equality comparison. Used by the
    deterministic DOC route's mechanical fidelity check."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _checkpoint_chars(prev: dict | None) -> int:
    """Read the wiki cursor, accepting the pre-delta lineage schema."""
    if not prev:
        return 0
    try:
        return int(prev.get("wiki_processed_chars", prev.get("chars")) or 0)
    except (TypeError, ValueError):
        return 0


def _checkpoint_hash(prev: dict | None) -> str | None:
    if not prev:
        return None
    return prev.get("wiki_prefix_hash") or prev.get("body_hash")


def _set_lineage_checkpoint(lineage, *, sid, raw_name, raw_body,
                            checkpoint_chars, slug, section, source_kind,
                            page_path):
    """Advance the wiki cursor after a durable apply.

    The cursor is a prefix of the cleaned raw body, not the size of the model
    input. This distinction is essential for chunked snapshots: a 50k slice
    may advance a 3.4M raw only to its slice end, never to ``len(slice)``.
    """
    try:
        checkpoint_chars = int(checkpoint_chars)
    except (TypeError, ValueError):
        return False
    if checkpoint_chars <= 0 or checkpoint_chars > len(raw_body):
        return False
    previous = lineage.get(sid) or {}
    if checkpoint_chars < _checkpoint_chars(previous):
        return False
    prefix = raw_body[:checkpoint_chars]
    try:
        page_text = Path(page_path).read_text(encoding="utf-8")
    except OSError:
        return False
    prefix_hash = _body_hash(prefix)
    lineage[sid] = {
        # ``chars``/``body_hash`` remain for old reports and vaults. The
        # explicit wiki_* names make it impossible to confuse this cursor with
        # the workspace checkpoint when both are inspected together.
        "chars": checkpoint_chars,
        "body_hash": prefix_hash,
        "wiki_processed_chars": checkpoint_chars,
        "wiki_prefix_hash": prefix_hash,
        "raw": raw_name,
        "raw_chars": len(raw_body),
        "raw_body_hash": _body_hash(raw_body),
        "last_raw": raw_name,
        "slug": slug,
        "section": section,
        "source_kind": source_kind,
        "page_body_hash": canon_mod.page_body_hash(page_text),
    }
    return True


def record_lineage_after_apply(vault, change, target_path):
    """Persist a session/doc cursor when a parked ChangeSet is later applied."""
    source = change.get("source") or {}
    sid = source.get("source_id") or source.get("session_id")
    checkpoint_chars = source.get("checkpoint_chars")
    if not sid or not checkpoint_chars:
        return False
    raw_path = canon_mod.raw_rel(vault, source.get("raw"))
    try:
        raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    _meta, raw_body = _read_frontmatter(raw_text)
    cls = change.get("classification") or {}
    lineage = _load_lineage(vault)
    updated = _set_lineage_checkpoint(
        lineage, sid=sid, raw_name=raw_path.name, raw_body=raw_body,
        checkpoint_chars=checkpoint_chars, slug=cls.get("slug"),
        section=cls.get("section") or "topics",
        source_kind=source.get("source_kind") or source.get("kind") or "session",
        page_path=target_path)
    if updated:
        _save_lineage(vault, lineage)
    return updated


def _is_strict_append(body: str, prev_chars: int, prev_hash: str | None) -> bool:
    """True when the first `prev_chars` chars of `body` hash to `prev_hash` —
    the append-only invariant (the workspace `incremental_source` prefix-hash
    pattern). A missing hash, a zero/negative/too-large offset, or a diverged
    prefix (edited/shrunk/reordered) all mean "not a strict append"."""
    return (bool(prev_hash) and 0 < prev_chars <= len(body)
            and _body_hash(body[:prev_chars]) == prev_hash)


def _append_delta(body: str, prev: dict | None) -> str | None:
    """The append-only tail of a re-captured raw vs its lineage checkpoint.

    Returns the new tail, or None when there is no usable checkpoint or the
    prefix diverged (edited/shrunk/reordered) — callers MUST fall back to a
    full snapshot, never fabricate a delta.
    """
    if not (prev and prev.get("slug")):
        return None
    prev_chars = _checkpoint_chars(prev)
    if not _is_strict_append(body, prev_chars, _checkpoint_hash(prev)):
        return None
    return body[prev_chars:]


def _delta_window(body: str, prev: dict | None, delta: str) -> tuple[int, int]:
    """Return absolute raw-body offsets covered by ``delta``."""
    start = _checkpoint_chars(prev)
    return start, start + len(delta)


def _delta_target_page(vault, prev: dict | None) -> Path | None:
    """The CURRENT canonical page a delta would merge into, or None (full fallback).

    A delta must merge into a live, current page. A page that was archived
    (status: archived), superseded, renamed (missing file), or never applied
    must NOT receive a blind append of the tail — fall back to a full merge so
    the proposer decides where the new content belongs (never a headless page).
    """
    if not (prev and prev.get("slug")):
        return None
    page = Path(vault) / "wiki" / f"{prev.get('section', 'topics')}" / f"{prev.get('slug')}.md"
    if not page.is_file():
        return None
    try:
        meta, _ = _read_frontmatter(page.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return None
    return page if meta.get("status", "current") == "current" else None


def _prepare_todo(vault, todo, synthed, synthed_path, vcfg, lim,
                  write_lock, _processed, _triage_log, _synthed_dirty=None,
                  _metrics=None):
    """Deterministic triage over the pending list, before a single LLM call.

    Returns a list of directive dicts:
      {"f", "h", "body", "kind", "mode", "slug", "delta"}
    mode == "full"  → the normal propose→merge→verify path.
    mode == "delta" → skip propose (slug known from lineage), merge ONLY the
                      new tail into the existing page, then verify.
    Items that triage to "skip" (superseded snapshot, config-skipped source,
    or an append with no material change) are marked done in `synthed` here.
    """
    from .ingest import _matches_any
    skip_globs = (((vcfg or {}).get("ingest") or {}).get("docs") or {}).get("skip_ids") or []
    lineage = _load_lineage(vault)
    changed = False

    by_id = {}
    for f, h in todo:
        text = f.read_text(encoding="utf-8", errors="ignore")
        meta, body = _read_frontmatter(text)
        # Pipeline feedback loop: the capture hook snapshotted one of the memex's
        # own `claude -p` workers (propose/merge/workspace) running in the runner's
        # temp cwd. The source session/doc it processed exists as its own raw, so
        # this capture adds no durable knowledge — skip it without an LLM call.
        # Marked done in synthed so it never re-enters the backlog. A toggle
        # (`limits.skip_pipeline_artifacts`) allows re-enabling if ever needed.
        if (lim or {}).get("skip_pipeline_artifacts", True) and _is_pipeline_artifact(meta, body):
            with write_lock:
                synthed[f.name] = h
                _processed[0] += 1
            _triage_log.append(
                f"triage: {f.name} skipped (pipeline artifact — memex worker capture)")
            if _metrics is not None:
                _metrics.append({"fname": f.name,
                                 "kind": meta.get("kind", "session"),
                                 "mode": "skipped-meta-worker",
                                 "route": "superseded", "outcome": "superseded",
                                 "reason": "captured the memex's own synthesis worker"})
            changed = True
            continue
        sid = meta.get("id") or f.stem
        by_id.setdefault(sid, []).append({
            "f": f, "h": h, "body": body, "kind": meta.get("kind", "session"),
        })

    prepared = []
    for sid, snaps in by_id.items():
        if len(snaps) > 1:
            # Same id captured N times (PreCompact snapshots, re-captures).
            # Keep the NEWEST capture (a doc may be edited DOWN — a trimmed
            # current version must not be superseded by its older longer self);
            # body length is only a tie-break for captures within the same
            # second. The rest are superseded.
            snaps.sort(key=lambda s: (s["f"].stat().st_mtime, len(s["body"])),
                       reverse=True)
            keeper, extras = snaps[0], snaps[1:]
            for extra in extras:
                with write_lock:
                    synthed[extra["f"].name] = extra["h"]
                    _processed[0] += 1
                _triage_log.append(
                    f"triage: {extra['f'].name} superseded by newer snapshot of {sid[:32]}")
                if _metrics is not None:
                    _metrics.append({"fname": extra["f"].name, "kind": extra["kind"],
                                     "mode": "superseded-snapshot",
                                     "route": "superseded", "outcome": "superseded",
                                     "reason": "duplicate snapshot"})
                changed = True
        else:
            keeper = snaps[0]
        f, h, body, kind = keeper["f"], keeper["h"], keeper["body"], keeper["kind"]

        # Config-driven source skip (e.g. a personal automation log).
        if skip_globs and _matches_any(Path(str(sid)), skip_globs):
            with write_lock:
                synthed[f.name] = h
                _processed[0] += 1
            _triage_log.append(f"triage: {f.name} skipped (config skip_ids)")
            if _metrics is not None:
                _metrics.append({"fname": f.name, "kind": kind,
                                 "mode": "skipped-config",
                                 "route": "superseded", "outcome": "superseded",
                                 "reason": "config skip_ids"})
            changed = True
            continue

        # Append-only re-capture (doc OR session) → delta merge against the
        # known page. Only when the lineage target page is a CURRENT canonical
        # page on disk: a page that was archived/renamed/superseded/never-
        # applied must NOT delta-merge into a headless or obsolete page — the
        # full fallback lets the proposer decide where the new content goes.
        prev = lineage.get(sid)
        delta = None
        tgt = _delta_target_page(vault, prev)
        if kind in ("doc", "session") and tgt is not None:
            delta = _append_delta(body, prev)
        if delta is not None:
            # Only a delta with NO content at all is superseded — a short-
            # but-material append (a decision, a correction) must never be
            # dropped on a length threshold; the verifier catches true
            # no-ops (`value: same`).
            if not delta.strip():
                with write_lock:
                    synthed[f.name] = h
                    _processed[0] += 1
                _triage_log.append(
                    f"triage: {f.name} superseded (append has no content)")
                if _metrics is not None:
                    _metrics.append({"fname": f.name, "kind": kind,
                                     "mode": "superseded-delta",
                                     "route": "superseded", "outcome": "superseded",
                                     "reason": "append no material change"})
                changed = True
                continue
            src, ck_slug, ck_section = delta, prev["slug"], prev.get("section", "topics")
        else:
            src, ck_slug, ck_section = body, None, None

        # Giant distill source (a >chunk_chars session or append tail) is split
        # into sequential chunks so the middle is never truncated by _excerpt —
        # each chunk proposes/merges/verifies independently (a giant working
        # session legitimately spans several wiki topics, so per-chunk routing
        # with the index's REUSE rule is healthier than forcing one page).
        cc = int((lim or {}).get("chunk_chars", 0) or 0)
        if cc > 0 and len(src) > cc:
            n = -(-len(src) // cc)
            for i in range(n):
                chunk_start = (_checkpoint_chars(prev) if delta is not None else 0) + i * cc
                prepared.append({
                    "f": f, "h": h, "body": body, "kind": kind, "sid": sid,
                    "mode": "chunk", "slug": ck_slug, "section": ck_section,
                    "delta": None,
                    "chunk": src[i * cc:(i + 1) * cc],
                    "chunk_index": i, "chunk_total": n, "chunk_of": f.name,
                    "chunk_start": chunk_start,
                    "chunk_from_delta": delta is not None,
                })
            continue
        if delta is not None:
            prepared.append({
                "f": f, "h": h, "body": body, "kind": kind, "sid": sid,
                "mode": "delta", "slug": ck_slug,
                "section": ck_section, "delta": delta,
                "checkpoint_start": _checkpoint_chars(prev),
            })
            continue
        prepared.append({"f": f, "h": h, "body": body, "kind": kind,
                         "mode": "full", "slug": None, "delta": None})

    if changed and _synthed_dirty is not None:
        _synthed_dirty[0] = True
    return prepared, lineage


def _apply_pending_auto(vault, outer_lock):
    """Auto-apply pending ChangeSets with verification.route == 'auto_apply'.

    These come from non-synth sources (e.g. analyze.py code architecture pages)
    that pre-set their route to auto_apply when vault auto_review is enabled.
    Called at the end of _run_impl while the synth lock is still held."""
    from . import changes as changes_mod
    pd = vault / ".memex" / "review" / "pending"
    if not pd.is_dir():
        return
    candidates = sorted(pd.glob("*.json"))
    if not candidates:
        return
    applied = 0
    for p in candidates:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        route = (d.get("verification") or {}).get("route", "review")
        if route != "auto_apply":
            continue
        cid = d.get("id")
        if not cid:
            continue
        try:
            result = changes_mod.apply_changeset(
                vault, cid, _lock=outer_lock, auto_review=True, defer_views=True)
            if result.get("state") == "applied":
                applied += 1
                print(f"  auto-applied pending ChangeSet {cid}")
            else:
                print(f"  pending auto-apply ChangeSet {cid}: {result.get('state')} ({result.get('reason', '')})")
        except Exception as e:
            print(f"  error applying ChangeSet {cid}: {e}")
    if applied:
        print(f"  auto-apply: {applied} pending ChangeSet(s) applied.")


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

    # Fresh per-run marker: a synthed/lineage flush that fails (ENOSPC/perms)
    # stays visible at the closing summary, never silently dropped.
    _failed_flushes.clear()

    lim = limits_mod.load(vault)
    vcfg = config_mod.load_vault(vault)
    models = config_mod.resolve_models(vault_cfg=vcfg)
    model_propose = getattr(args, "model_propose", None) or models.get("propose")
    model_merge = getattr(args, "model_merge", None) or models.get("merge")
    if not model_propose or not model_merge:
        print("error: no models configured. Set models.propose and models.merge "
              "in ~/.config/memex/config.json or run `memex doctor`.")
        return 1

    print(f"synth: propose={model_propose}  merge={model_merge}")

    # Newest capture FIRST: a reflect spawned by a hook synthesizes the session
    # that was just compacted/exited before the historical backlog. mtime is the
    # capture time (filename is session-date, not capture time).
    raw_files = sorted(canon_mod.raw_dir(vault).glob("*.md"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
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
    # --priority (the just-captured session) goes FIRST regardless of mtime —
    # guarantees the current session is synthesized before the historical backlog.
    if getattr(args, "priority", None):
        for i, (f, h) in enumerate(todo):
            if f.name == args.priority:
                todo.insert(0, todo.pop(i))
                break
    # Giants (> auto_drain_max_chars) go LAST — a re-captured giant session
    # (compaction accumulation) must not block the normal backlog. Stable sort
    # so the priority raw stays first within its tier; a giant priority raw
    # waits for the normal tier (its turn comes as the giant tier drains).
    cap = int(lim.get("auto_drain_max_chars", 200000) or 0)
    if cap > 0 and len(todo) > 1:
        todo.sort(key=lambda fh: os.path.getsize(fh[0]) > cap)
    if getattr(args, "limit", None):
        todo = todo[: args.limit]

    if not todo:
        print("nothing new to synthesize.")
        return 0
    print(f"{len(todo)} raw note(s) to process.\n")

    # Source lineage: remembered per source-id as pages are produced, so a future
    # append-only re-capture can delta-merge. Initialized before triage; written
    # at the end of the run (see `_lineage_dirty`).
    lineage = _load_lineage(vault)
    _lineage_dirty = [False]
    _synthed_dirty = [False]
    _triage_log = []
    _metrics = []
    # M2 dedup: ChangeSets ALREADY pending for a raw are loaded once at run
    # start. A raw whose prior reflect was killed after save_changeset but
    # before the synthed flush is still in review — reprocessing it must not
    # stack a duplicate ChangeSet (one slice once had 11 identical pendings).
    dedup_set = changes_mod.load_pending_dedup(vault)
    # M3 retry cap: per-raw provider-failure counters (cleared on success,
    # parked at the cap) so a persistently-failing raw never reprocesses
    # forever. Loaded once at run start; shared by all workers via closure.
    attempts = _load_attempts(vault)

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
    _created = [0]       # ChangeSets saved this run (applied or pending)
    _applied = [0]       # ChangeSets auto-applied this run
    _pending = [0]       # ChangeSets left pending review this run
    _stop = [False]       # circuit-breaker flag (checked by workers before LLM calls)
    # Chunked sessions: which chunk indices of each raw were durably handled
    # (applied / parked / skipped / judged-rejected). A raw is marked done only
    # when ALL its chunks are present here — an errored chunk keeps it pending.
    _chunk_done_map = {}
    chunked_items = []

    # Cap concurrent STRONG-judge calls (verify_model is the expensive one). A
    # 4-worker run could otherwise fire 4 strong calls at once; 0 = uncapped.
    vw = int(lim.get("verify_workers", 2) or 0)
    verify_sem = threading.BoundedSemaphore(min(vw, workers)) if vw > 0 else None

    # Deterministic triage (needs the counters/lock above): collapse same-id
    # snapshots, drop config-skipped sources, route append-only re-captures to
    # a delta merge.
    todo, lineage = _prepare_todo(
        vault, todo, synthed, synthed_path, vcfg, lim, write_lock,
        _processed, _triage_log, _synthed_dirty, _metrics)
    for msg in _triage_log:
        print(f"  {msg}")
    if not todo:
        print("nothing new after triage.")
        # Triage may have superseded/skipped raws in-memory — flush those marks
        # AND the triage metrics before returning, or they'd re-enter the
        # backlog / be lost every run.
        _flush_state(vault, synthed,
                     synthed_path if _synthed_dirty[0] else None,
                     lineage, idx, _metrics)
        return 0
    chunked_items[:] = [it for it in todo if it.get("chunk") is not None]
    print(f"{len(todo)} raw note(s) after triage"
          f" ({len(chunked_items)} chunk slices).\n")

    # `_prune_wikilinks` validates against canonical slugs ONLY (a hallucinated
    # link to a page that isn't a current canonical page is unwrapped). Since
    # apply_changeset is the only writer of wiki/, pages proposed this run only
    # become canonical once their ChangeSet is applied.
    canonical_slugs = {p["slug"] for p in canon_mod.canonical_pages(vault, idx)}
    # `run()` already holds the per-vault lock while `_run_impl` executes; hand
    # it to apply_changeset so the promoter reuses the SAME lock instead of
    # deadlocking on itself ("vault busy").
    outer_lock = vault / ".memex" / "synth.lock"

    # Deep-copy the index as a snapshot so ALL parallel proposes see the same
    # picture of the brain. This differs from the sequential loop (where each
    # propose sees the progressively updated index), but is safe: the propose
    # step only suggests a slug; the merge step reads the actual existing body
    # from disk (which may already include another worker's merge). Two notes
    # proposing the same new slug is fine — the second merge integrates into
    # the page the first one created.
    idx_snapshot = json.loads(json.dumps(idx))

    total = len(todo)

    def _process_one(item, idx_at_start):
        """Propose → merge → verify → ChangeSet (page mutation only through the
        promoter; the write phase is serialized via write_lock). `item` is a
        triage directive from `_prepare_todo` (mode full | delta)."""
        # circuit breaker check before any LLM call
        if _stop[0]:
            return None
        f, h, mode = item["f"], item["h"], item["mode"]
        _t0 = time.time()

        raw_full = f.read_text(encoding="utf-8")
        meta, body = _read_frontmatter(raw_full)
        source, sid = meta.get("source", "doc"), meta.get("id", f.stem)
        note_kind = meta.get("kind", "session")
        raw_body = body
        # A chunk directive processes ONE bounded slice of a giant session's
        # body. The full raw is still read for claim-anchoring (raw_lines below)
        # and for the absolute lineage cursor; only the distilled source is the
        # chunk. `synthed` is NOT set here — the raw is marked done only when
        # ALL its chunks are durably handled (post-dispatch pass).
        is_chunk = item.get("chunk") is not None
        attempt_key = (f.name + "#" + str(item.get("chunk_index"))) if is_chunk else f.name
        if is_chunk:
            body = item["chunk"]
        is_delta = mode == "delta"

        # ── deterministic DOC adoption ─────────────────────────────────────
        # A captured doc is ALREADY-curated prose (README, ADR, skill, note).
        # The propose→merge→verify LLM round-trip adds cost and can distort the
        # author's wording; a verbatim adoption with a containment check is
        # faithful by construction. We only take this route when the doc yields
        # a title AND a stable path-derived slug — otherwise fall through to the
        # normal LLM path (the ADOPT prompt). Docs are never chunked (no >50k
        # docs in the corpus), so this is full-mode only.
        doc_auto = (note_kind == "doc" and mode == "full" and not is_chunk
                    and (lim or {}).get("doc_deterministic_route", True))
        if doc_auto:
            doc_title, doc_clean, doc_tags = _doc_parts(body)
            doc_slug = _doc_slug(sid, meta.get("cwd") or "",
                                 lim.get("slug_max", 60))
            if not (doc_title and doc_clean and doc_slug):
                doc_auto = False  # can't derive identity → fall back to LLM ADOPT
        checkpoint_start = int(item.get("checkpoint_start") or item.get("chunk_start") or 0)
        checkpoint_end = checkpoint_start + len(item.get("delta") or item.get("chunk") or "")
        def _record_chunk_done():
            """Mark this chunk durably handled. Call ONLY under write_lock.
            `item` is the chunk directive (never rebound — see the claims loop
            below, which uses `ev` as its loop var so this closure keeps the
            real chunk item)."""
            if is_chunk:
                _chunk_done_map.setdefault(item["chunk_of"], set()).add(item["chunk_index"])
        # Two excerpt budgets: the cheap propose classifier only needs enough to
        # route (slug/section/tags) — a coarse decision — while the merge needs
        # the full budget where content fidelity actually lives. For a long
        # session this cuts propose input ~4x with no routing loss.
        propose_excerpt = _excerpt(body, lim.get("raw_propose_chars") or lim["raw_excerpt_chars"])
        merge_excerpt = _excerpt(body, lim["raw_excerpt_chars"])
        raw_lines = raw_full.splitlines()

        # ── phase 1: propose (parallel, readonly; SKIPPED for delta) ──
        # Single propose model (model_propose) for all session sizes. The old
        # two-tier propose (dense/cheap) existed solely so the stronger model
        # produced verbatim quotes for the quote-match gate — but unanchored
        # claims (paraphrased quotes) now proceed to body-fidelity verification
        # instead of being parked. No need for a second propose model.
        _propose_model = model_propose
        if mode == "delta":
            # Append-only re-capture of an already-processed doc: the slug/section
            # come from lineage — no propose call, no index scan.
            prop = {"slug": item["slug"], "section": item.get("section", "topics"),
                    "title": None, "tags": [], "related": [], "distill": None,
                    "claims": []}
            merge_excerpt = _excerpt(item["delta"], lim["raw_excerpt_chars"])
        elif doc_auto:
            # Deterministic DOC route: identity derived from the source path +
            # H1 — no propose call, no index scan, no LLM at all. The body IS
            # the adopted content (verbatim); the verifier does a containment
            # check instead of a judge model.
            prop = {"slug": doc_slug, "title": doc_title,
                    "section": "topics", "tags": doc_tags,
                    "related": [], "distill": None, "claims": []}
        else:
            _propose_model = model_propose
            try:
                p1 = providers.complete(
                    PROPOSE_PROMPT.format(about=about,
                                          index=_index_summary(idx_at_start, propose_excerpt,
                                                               lim.get("index_neighbors", 0)),
                                          source=source, kind=note_kind, raw=propose_excerpt),
                    model=_propose_model)
            except Exception as e:
                with write_lock:
                    _errored[0] += 1
                    _err_cnt[0] += 1
                    _processed[0] += 1
                    print(f"  [{_processed[0]}/{total}] {f.name}: provider error: {e} — skipping (stays pending)")
                    _cap = lim.get("provider_error_cap", 3)
                    if _cap and _record_attempt(vault, attempts, attempt_key) >= _cap:
                        _park_raw(vault, synthed, synthed_path, lineage, attempts,
                                  f.name, h, raw_path=f,
                                  reason=f"parked after {_cap} provider errors",
                                  is_chunk=is_chunk, record_chunk_done=_record_chunk_done,
                                  attempt_key=attempt_key)
                        print(f"  [{_processed[0]}/{total}] {f.name} -> PARKED (provider errors x{_cap})")
                        _metrics.append({
                            "fname": f.name, "kind": note_kind, "mode": "parked-provider-error",
                            "outcome": "parked", "route": "park", "reason": "provider error cap",
                            "latency_ms": int((time.time() - _t0) * 1000), "body_chars": len(body),
                            "model_propose": _propose_model, "model_merge": model_merge,
                            "verify_model": config_mod.resolve_verify_model(vcfg, default=model_merge),
                        })
                    if _err_cnt[0] >= 5:
                        _stop[0] = True
                return None

            prop = _extract_json(p1) or {}

            # ── skip check ──
            if prop.get("skip"):
                with write_lock:
                    _processed[0] += 1
                    _err_cnt[0] = 0
                    if not is_chunk:
                        _mark_done(vault, synthed, synthed_path, lineage, f.name, h)
                        _synthed_dirty[0] = True
                    else:
                        _record_chunk_done()
                    # M3: skip is terminal — clear any accumulated provider-error
                    # counter so a stale count never pushes a later reprocess
                    # (after a manual synthed reset) toward the park cap too early.
                    _clear_attempt(vault, attempts, attempt_key)
                    print(f"  [{_processed[0]}/{total}] {f.name}"
                          + (f" chunk {item['chunk_index'] + 1}/{item['chunk_total']}" if is_chunk else "")
                          + ": skipped (no durable knowledge)")
                return None

        # ── resolve slug, section, related, project (readonly, no lock) ──
        slug = (
            _kebab(prop.get("slug")) or _kebab(prop.get("title"))
            or _kebab((prop.get("distill") or "")[:50]) or f"note-{str(sid)[:8]}"
        )[:lim["slug_max"]].strip("-") or f"note-{str(sid)[:8]}"
        section = prop.get("section") if prop.get("section") in ("topics", "entities", "decisions") else "topics"
        # related slugs: only proposal-returned slugs that already exist (no
        # lexical fallback — links must be explicitly relevant, zero allowed)
        related = [_kebab(r) for r in (prop.get("related") or [])
                   if isinstance(r, str) and _kebab(r) in pages_by_slug]

        # ── phase 2: merge (parallel, readonly) ──
        # Read existing body from DISK — another worker may have created/updated
        # this page since the snapshot, so we read the latest on-disk state.
        # We do NOT hold the lock here (the LLM call is expensive); a brief
        # stale read is harmless because the merge handles integration.
        existing_pre = pages_by_slug.get(slug)
        page_path_pre = (vault / "wiki" / existing_pre["path"]) if existing_pre else (vault / "wiki" / section / f"{slug}.md")
        # Delta safety: a delta directive's lineage target may have vanished
        # from the INDEX or from DISK (archived/renamed/never-applied). Merging
        # the appended TAIL alone would build a headless page missing the base
        # content — fall back to a FULL merge of the whole raw under the lineage
        # slug instead (never a headless page).
        if mode == "delta" and (existing_pre is None or not page_path_pre.exists()):
            mode = "full"
            merge_excerpt = _excerpt(body, lim["raw_excerpt_chars"])
        # Pipeline label for telemetry — computed AFTER the delta→full fallback
        # so it reflects the mode actually used. "full" | "doc-delta" |
        # "session-delta" | "chunk". A delta metric carries the tail size
        # (delta_chars) and the pre-raw checkpoint; `checkpoint_after` is added
        # only when the delta actually applies (the lineage write is the real
        # checkpoint). A chunk metric carries the slice window.
        if mode == "delta":
            _dmode = f"{note_kind}-delta"
            _dlen = len(item["delta"])
            _ckpt = {"delta_chars": _dlen,
                     "checkpoint_before": len(body) - _dlen}
        elif is_chunk:
            _dmode = "chunk"
            _ckpt = {"chunk_index": item["chunk_index"],
                     "chunk_total": item["chunk_total"],
                     "chunk_chars": len(body)}
        elif doc_auto:
            # Deterministic DOC adoption — zero LLM calls; the metric carries
            # the char budget it would have consumed for comparison.
            _dmode, _ckpt = "doc-auto", {"body_chars": len(doc_clean or "")}
        else:
            _dmode, _ckpt = "full", {}
        existing_full_pre = page_path_pre.read_text(encoding="utf-8") if page_path_pre.exists() else ""
        _, existing_body_pre = _read_frontmatter(existing_full_pre)

        merge_prompt = ADOPT_MERGE_PROMPT if source == "doc" else DISTILL_MERGE_PROMPT
        if doc_auto:
            # Deterministic DOC route: no merge LLM call — the source prose IS
            # the adopted body. `doc_clean` is the raw body minus its internal
            # frontmatter (and minus a dup H1 when the title came from the
            # frontmatter). The exact original wording is preserved verbatim.
            merged_body = doc_clean
        else:
            merge_kwargs = dict(
                existing=existing_body_pre or "(none yet)", source=source, sid=sid,
                raw=merge_excerpt, raw_fname=f.name,
                related=", ".join(f"[[{r}]]" for r in related) or "(none)")
            if merge_prompt is DISTILL_MERGE_PROMPT:
                merge_kwargs["about"] = about
            try:
                merged_body = providers.complete(
                    merge_prompt.format(**merge_kwargs),
                    model=model_merge)
            except Exception as e:
                with write_lock:
                    _errored[0] += 1
                    _err_cnt[0] += 1
                    _processed[0] += 1
                    print(f"  [{_processed[0]}/{total}] {f.name}: provider error: {e} — skipping (stays pending)")
                    _cap = lim.get("provider_error_cap", 3)
                    if _cap and _record_attempt(vault, attempts, attempt_key) >= _cap:
                        _park_raw(vault, synthed, synthed_path, lineage, attempts,
                                  f.name, h, raw_path=f,
                                  reason=f"parked after {_cap} provider errors",
                                  is_chunk=is_chunk, record_chunk_done=_record_chunk_done,
                                  attempt_key=attempt_key)
                        print(f"  [{_processed[0]}/{total}] {f.name} -> PARKED (provider errors x{_cap})")
                        _metrics.append({
                            "fname": f.name, "kind": note_kind, "mode": "parked-provider-error",
                            "outcome": "parked", "route": "park", "reason": "provider error cap",
                            "latency_ms": int((time.time() - _t0) * 1000), "body_chars": len(body),
                            "model_propose": _propose_model, "model_merge": model_merge,
                            "verify_model": config_mod.resolve_verify_model(vcfg, default=model_merge),
                        })
                    if _err_cnt[0] >= 5:
                        _stop[0] = True
                return None

        merged_body = _clean_body(merged_body)
        # wikilinks are only valid against CANONICAL slugs — a link to a page
        # that isn't a current canonical page is unwrapped (kills hallucinations)
        merged_body = _prune_wikilinks(merged_body, canonical_slugs)
        merged_body = _dedup_blocks(merged_body)

        # ── claims + source-relative anchors → absolute raw anchors ──
        # NOTE: the loop variable is `ev`, NOT `item` — `item` is the triage
        # directive (the chunk directive for a giant session) and `_record_chunk_done`
        # closes over it by reference. Reusing `item` here would rebind it to the
        # last evidence dict ({quote, start_line, end_line}), so a chunk that
        # applied/parked would fail to mark done (KeyError: chunk_of) and the raw
        # would stay pending forever.
        claims = []
        for c in (prop.get("claims") or []):
            text = str(c.get("text") or "").strip()
            anchors = []
            for ev in (c.get("evidence") or []):
                quote = str(ev.get("quote") or "").strip() or text
                anchor = _resolve_anchor(raw_lines, quote, f)
                if anchor:
                    anchors.append(anchor)
            if not anchors:
                anchor = _resolve_anchor(raw_lines, text, f)
                if anchor:
                    anchors.append(anchor)
            if not anchors and note_kind != "doc":
                # Session distillation: a claim without a verbatim anchor is
                # STILL a valid claim (paraphrased, not hallucinated). The
                # body-fidelity check in verify_fidelity will decide. No
                # longer parked as ambiguous — unanchored claims in FAITHFUL_
                # OUTCOMES let them proceed to the verifier.
                pass
            claims.append({
                "text": text,
                "type": str(c.get("type") or "process"),
                "explicitness": str(c.get("explicitness") or "inferred"),
                "evidence": anchors,
            })

        # ── build the ChangeSet (semantic identity + proposed body) ──
        existing = existing_pre
        project = (existing.get("project") if existing else None) or _resolve_project(meta.get("cwd"), prop)
        page_path = (vault / "wiki" / existing["path"]) if existing else page_path_pre
        existing_full = existing_full_pre
        rel = str(page_path.relative_to(vault / "wiki"))

        raw_kind = meta.get("kind", "session")
        if raw_kind not in KIND_RANK:
            raw_kind = "session"
        new_kind = raw_kind
        if existing and KIND_RANK.get(existing.get("kind", "session"), 1) <= KIND_RANK.get(raw_kind, 1):
            new_kind = existing.get("kind", "session")
        new_status = existing.get("status", "current") if existing else "current"
        new_superseded_by = existing.get("superseded_by") if existing else None
        src_ref = f"{source}:{sid}"
        sources = list(dict.fromkeys((existing.get("sources", []) if existing else []) + [src_ref]))
        tags = _clean_tags((existing.get("tags", []) if existing else []) + (prop.get("tags") or []), max_tags=lim["max_tags"])
        title = (existing.get("title") if existing else None) or prop.get("title") or slug
        page_record = {
            "slug": slug, "title": title,
            "section": (existing.get("section", section) if existing else section),
            "kind": new_kind, "status": new_status,
            "tags": tags, "sources": sources, "project": project,
            "summary": _summary_from(prop.get("distill") or (existing.get("summary") if existing else "") or ""),
            "path": rel,
        }
        if new_superseded_by:
            page_record["superseded_by"] = new_superseded_by

        # The ChangeSet source kind mirrors the raw note's real kind (doc for
        # adopted documents, session/raw for distillations). Hardcoding "raw"
        # here misrouted adopted docs through the strict session quote-match
        # gate; the doc-ADOPT body-fidelity path keys on source.kind == "doc".
        src_kind = "raw"
        if note_kind == "doc":
            src_kind = "doc"
        # `mode` travels on the ChangeSet so the verifier/classifier/promoter
        # know this proposal is a verified delta (body-fidelity vs the tail,
        # no per-claim anchors) and can route it accordingly.
        source_payload = {"raw": f"raw/{f.name}", "raw_sha256": canon_mod.file_hash(f),
                          "kind": src_kind, "mode": mode}
        target_payload = {"slug": slug}
        if existing_full:
            target_payload["expected_page_sha256"] = canon_mod.page_body_hash(existing_full)
        change = changes_mod.new_changeset(
            operation="update" if existing_full else "create",
            classification={"section": section, "slug": slug, "title": title, "project": project},
            source=source_payload,
            target=target_payload,
            claims=claims,
            proposed_body=merged_body,
            risk="low",
            reason="synth proposal",
        )
        change["index_record"] = page_record
        # M2: carry the slice index so `compute_dedup_key` distinguishes chunks
        # of the same raw (None for a full/delta change → serialized as "").
        change["_chunk_index"] = item.get("chunk_index")

        # ── verification + risk (parallel, readonly — the extra complete call) ──
        auto_review = bool(vcfg.get("auto_review", False))
        # model_verify is the single verify model — no cheap/flash judge, no
        # separate verify_chunk_model. Mechanical pre-verify checks (empty body,
        # same-as-current, containment) skip the LLM when they can prove fidelity.
        verify_model = models.get("verify") or models.get("merge")
        v_chars = lim.get("verify_source_chars", 12000)

        # A SLICE (delta or chunk) is judged by BODY FIDELITY against the exact
        # source window being incorporated — the appended tail, or the chunk
        # slice — NOT by per-claim quote-anchors. Chunked distillation exposed
        # why: propose returns some claims without verbatim quotes (~37% in
        # prod), and the all-or-nothing `ungrounded` gate parked 99.5% of chunks
        # as ambiguous even when the merge was faithful (2836/2851 in the last
        # full run). Slices follow the delta contract: the verifier compares the
        # distilled body to the slice itself, and only `supported` auto-applies.
        # The per-claim deterministic gates below apply ONLY to full-session
        # distillation, where the whole raw is the source and quote-matching is
        # meaningful. (validate_evidence is skipped for slices — its unsupported
        # verdicts on unanchored claims would otherwise trip the evidence gate
        # and archive a faithful merge.)
        # ── mechanical pre-verify (structural checks, 0 LLM) ──
        is_slice = mode in ("delta", "chunk")
        merged_norm = _normalize_ws(merged_body)
        existing_norm = _normalize_ws(existing_body_pre) if existing_body_pre else ""

        # doc_auto: keep the deterministic containment check (never calls LLM).
        if doc_auto:
            raw_norm = _normalize_ws(raw_body)
            if not merged_norm or (raw_norm and merged_norm not in raw_norm):
                evidence = []
                verification = {"outcome": ctr.Outcome.AMBIGUOUS,
                                "reason": "doc body not contained in source (parse mismatch)"}
            else:
                evidence = []
                value = ctr.Value.NEW
                if existing_norm and existing_norm == merged_norm:
                    value = ctr.Value.SAME
                verification = {"outcome": ctr.Outcome.SUPPORTED, "value": value,
                                "reason": "deterministic doc adoption (verbatim containment)"}
        else:
            v_src = None
            if is_slice:
                v_src = item.get("delta") or item.get("chunk")
            else:
                v_src = raw_body
            v_src_norm = _normalize_ws(v_src) if v_src else ""

            # Check 1: empty body or only whitespace
            if not merged_norm:
                evidence = []
                verification = {"outcome": ctr.Outcome.AMBIGUOUS, "value": ctr.Value.META,
                                "reason": "proposed body is empty"}
            # Check 2: no-op — body unchanged from current page
            elif existing_norm and merged_norm == existing_norm:
                evidence = []
                verification = {"outcome": ctr.Outcome.SUPPORTED, "value": ctr.Value.SAME,
                                "reason": "body unchanged from current page (no-op)"}
            # Check 3: containment — body is a faithful subset of the source
            # (works for doc adoption and small slices where the merge output
            # preserves source content near-verbatim; distillation drops noise
            # but the core knowledge should be a substring).
            elif v_src_norm and merged_norm in v_src_norm:
                evidence = []
                value = ctr.Value.NEW
                if existing_norm and existing_norm == merged_norm:
                    value = ctr.Value.SAME
                verification = {"outcome": ctr.Outcome.SUPPORTED, "value": value,
                                "reason": "mechanical containment: body contained in source"}
            else:
                # ── verify LLM (single model_verify) when mechanical cannot decide ──
                if is_slice:
                    # A slice (delta/chunk) is body-judged against its window.
                    evidence = []
                else:
                    evidence = verify_mod.validate_evidence(vault, change)
                if not claims and not is_slice:
                    verification = {"outcome": ctr.Outcome.AMBIGUOUS,
                                    "reason": "no extractable claims to verify"}
                elif verify_mod.evidence_blocks(evidence):
                    verification = {
                        "outcome": ctr.Outcome.UNSUPPORTED,
                        "reason": "claim evidence not anchored in source",
                        "route": ctr.Route.REJECT if auto_review else ctr.Route.ARCHIVE,
                    }
                else:
                    needs_strong = verify_mod.needs_strong_verify(
                        change, lim.get("verify_strong_body_chars", 8000))
                    if not needs_strong and is_slice:
                        # A slice that passed all gates but is small enough for
                        # cheap verify — but we have only one model now. The
                        # mechanical checks (empty/same/containment) already caught
                        # the obvious cases; anything that reaches here needs the
                        # LLM verify. Use model_verify for everything.
                        needs_strong = True
                    if verify_sem is not None and needs_strong:
                        with verify_sem:
                            verification = verify_mod.verify_fidelity(
                                vault, change, model=verify_model,
                                source_text=v_src,
                                source_chars=v_chars)
                    else:
                        verification = verify_mod.verify_fidelity(
                            vault, change, model=verify_model,
                            source_text=v_src,
                            source_chars=v_chars)
    
        # A verifier that failed to answer (model down / unparseable JSON) is an
        # INFRA retry, not a content verdict. `error=True` keeps the raw pending
        # so it re-runs next reflect instead of being discarded as a rejection
        # (in auto-review mode reject/archive permanently marks the raw done).
        if verification.get("error"):
            with write_lock:
                _processed[0] += 1
                _err_cnt[0] += 1
                _errored[0] += 1
                print(f"  [{_processed[0]}/{total}] {f.name}: verifier unavailable "
                      f"({verification.get('reason', 'error')}) — staying pending")
                _cap = lim.get("provider_error_cap", 3)
                if _cap and _record_attempt(vault, attempts, attempt_key) >= _cap:
                    _park_raw(vault, synthed, synthed_path, lineage, attempts,
                              f.name, h, raw_path=f,
                              reason=f"parked after {_cap} provider errors",
                              is_chunk=is_chunk, record_chunk_done=_record_chunk_done,
                              attempt_key=attempt_key)
                    print(f"  [{_processed[0]}/{total}] {f.name} -> PARKED (provider errors x{_cap})")
                    _metrics.append({
                        "fname": f.name, "kind": note_kind, "mode": "parked-provider-error",
                        "outcome": "parked", "route": "park", "reason": "provider error cap",
                        "latency_ms": int((time.time() - _t0) * 1000), "body_chars": len(body),
                        "model_propose": _propose_model, "model_merge": model_merge,
                        "verify_model": verify_model,
                    })
                if _err_cnt[0] >= 5:
                    _stop[0] = True
            return None
        verification["route"] = verify_mod.classify_risk(change, evidence, verification,
                                                          auto_review=auto_review)
        change["verification"] = verification

        # ── phase 3: route (serial, under lock) ──
        _applied_this = False
        with write_lock:
            cid = change["id"]
            # In auto-review mode, `reject` means the verifier decided this is
            # hallucinated/meta/noop content — discard the raw (mark done, no
            # ChangeSet). `archive` (unsupported) also discards in auto-review.
            if verification["route"] in ("reject", "archive") and auto_review:
                _processed[0] += 1
                _err_cnt[0] = 0
                if not is_chunk:
                    _mark_done(vault, synthed, synthed_path, lineage, f.name,
                               hashlib.sha256(f.read_bytes()).hexdigest()[:16])
                    _synthed_dirty[0] = True
                else:
                    _record_chunk_done()
                # M3: auto-reject is terminal — clear any accumulated provider-error
                # counter so a stale count never pushes a later reprocess toward
                # the park cap after one blip.
                _clear_attempt(vault, attempts, attempt_key)
                # Fix 2: a SINGLE delta rejected in hands-free is terminal — the
                # discarded tail must never be re-proposed. Advance the cursor to
                # the end (the whole delta was seen). A CHUNKED delta is handled
                # by the finally block (cursor advances only when ALL slices are
                # durably handled). Non-auto (review) never advances.
                if mode == "delta":
                    if _advance_delta_cursor(lineage, sid=sid, cursor=len(body),
                                             raw_body=body):
                        _lineage_dirty[0] = True
                print(f"  [{_processed[0]}/{total}] {f.name} -> auto-rejected "
                      f"({verification.get('reason', verification['route'])})")
                _metrics.append({
                    "fname": f.name, "kind": note_kind, "mode": _dmode,
                    "outcome": verification.get("outcome"),
                    "route": str(verification["route"]),
                    "reason": str(verification.get("reason", ""))[:200],
                    "latency_ms": int((time.time() - _t0) * 1000),
                    "body_chars": len(body),
                    "model_propose": _propose_model, "model_merge": model_merge,
                    "verify_model": verify_model, **_ckpt,
                })
                return None
            # M2: dedup — a reprocess of an already-in-review slice must not
            # create a duplicate; the raw is already handled, mark it done.
            def _dedup_skip(reason):
                if not is_chunk:
                    _mark_done(vault, synthed, synthed_path, lineage, f.name, h)
                    _synthed_dirty[0] = True
                    # Fix 2: a SINGLE delta dedup-skipped in hands-free is terminal
                    # (its slice is already durably represented) — advance cursor so
                    # it's not re-proposed. Chunks advance via the finally block.
                    if mode == "delta" and _advance_delta_cursor(
                            lineage, sid=sid, cursor=len(body), raw_body=body):
                        _lineage_dirty[0] = True
                else:
                    _record_chunk_done()
                # M3: dedup-skip is terminal — clear any accumulated provider-error
                # counter (this closure shares the run's `attempts` dict).
                _clear_attempt(vault, attempts, attempt_key)
                _processed[0] += 1
                _err_cnt[0] = 0
                print(f"  [{_processed[0]}/{total}] {f.name} -> dedup-skip ({reason})")
                _metrics.append({
                    "fname": f.name, "kind": note_kind, "mode": "dedup-skip",
                    "outcome": "dedup", "route": "skip",
                    "reason": reason, "latency_ms": 0,
                    "body_chars": len(body),
                    "model_propose": _propose_model, "model_merge": model_merge,
                    "verify_model": verify_model,
                })

            _dk = changes_mod.compute_dedup_key(change)
            _existing = dedup_set.get(_dk)
            if _existing is not None:
                _ex = None
                try:
                    _ex = changes_mod.load_changeset(vault, _existing)
                except Exception:
                    _ex = None
                if _ex:
                    _ex_state = _ex[0].get("state")
                    _diverged = (_body_hash(_ex[0].get("proposed_body", ""))
                                 != _body_hash(change.get("proposed_body", "")))
                    if not _diverged:
                        # idêntico — skip: o raw já está em review (o run anterior
                        # foi morto entre save_changeset e o flush do synthed).
                        # Seguro em qualquer estado (pending/applied/rejected/…).
                        _dedup_skip(f"identical {_ex_state} exists")
                        return None
                    if _ex_state != "pending":
                        # divergido MAS resolvido (applied/rejected/stale/...): a
                        # change já foi decidida por um reviewer concorrente entre
                        # o load do dedup_set (início do run) e este route. Supersedê-la
                        # relocaria uma applied/… para stale — corrompe o ledger de
                        # estados (rollback/health contariam errado). Skip, sem duplicar.
                        _dedup_skip(f"existing {_ex_state} not superseded")
                        return None
                    # divergido e ainda pending — supersede o antigo (old -> stale)
                    # e cai para salvar o novo abaixo.
                    try:
                        changes_mod.transition_changeset(
                            vault, _existing, "stale", reason="superseded by reprocess")
                    except Exception:
                        pass
            changes_mod.save_changeset(vault, change)
            dedup_set[_dk] = change["id"]
            _created[0] += 1  # durably saved — applied or parked pending
            if is_chunk:
                _record_chunk_done()  # applied OR parked → durably handled
            if verification["route"] == "auto_apply":
                result = changes_mod.apply_changeset(vault, cid, _lock=outer_lock,
                                                     auto_review=auto_review, defer_views=True)
                if result.get("state") == "applied":
                    # Source lineage — recorded ONLY when the page actually
                    # exists (applied). A pending/rejected ChangeSet must not
                    # poison lineage with a slug that has no page, or the next
                    # append-only re-capture would delta-merge into nothing.
                    lineage[sid] = {
                        "raw": f.name,
                        "chars": len(body),
                        "body_hash": _body_hash(body),
                        "slug": slug,
                        "section": section,
                        "source_kind": note_kind,
                        # hash of the canonical page body right after this apply —
                        # lets the backfill dry-run tell externally-edited pages
                        # from unchanged ones.
                        "page_body_hash": canon_mod.page_body_hash(
                            page_path.read_text(encoding="utf-8")),
                    }
                    _lineage_dirty[0] = True
                    pages_by_slug[slug] = page_record
                    idx["pages"] = list(pages_by_slug.values())
                    with changelog.open("a", encoding="utf-8") as ch:
                        ch.write(json.dumps({
                            "ts": int(time.time()), "page": slug, "kind": new_kind,
                            "status": new_status,
                            "action": "create" if not existing_full else "update",
                            "source": f"{source}:{sid}", "raw": f.name}) + "\n")
                    _applied[0] += 1
                    _applied_this = True
                    print(f"  [{_processed[0] + 1}/{total}] {f.name} -> applied ChangeSet {cid} -> wiki/{rel}")
                else:
                    _pending[0] += 1
                    print(f"  [{_processed[0] + 1}/{total}] {f.name} -> pending ChangeSet {cid} "
                          f"({result.get('state')}: {result.get('reason') or result.get('error') or 'review required'})")
            else:
                _pending[0] += 1
                print(f"  [{_processed[0] + 1}/{total}] {f.name} -> pending ChangeSet {cid} (review required)")

            _err_cnt[0] = 0
            # M3: a successful finish clears this raw's provider-failure counter
            # so a transient blip never accumulates toward the park cap.
            _clear_attempt(vault, attempts, attempt_key)
            if not is_chunk:  # already inside `with write_lock:` (the route block)
                _mark_done(vault, synthed, synthed_path, lineage, f.name, h)
                _synthed_dirty[0] = True
            _processed[0] += 1

        # checkpoint_after reflects the ACTUAL lineage advancement: only an
        # applied delta advances it (a parked/pending/stale delta does not, and
        # the auto-reject path above never reached the route block).
        _ckpt_emit = dict(_ckpt)
        if mode == "delta" and _applied_this:
            _ckpt_emit["checkpoint_after"] = len(body)
        _metrics.append({
            "fname": f.name, "kind": note_kind, "mode": _dmode,
            "outcome": verification.get("outcome"),
            "route": str(verification["route"]),
            "reason": str(verification.get("reason", ""))[:200],
            "latency_ms": int((time.time() - _t0) * 1000),
            "body_chars": len(body),
            "model_propose": _propose_model, "model_merge": model_merge,
            "verify_model": verify_model, **_ckpt_emit,
        })
        return f.name

    # ── dispatch ──
    try:
        if workers <= 1:
            # Sequential fallback: no thread-pool overhead, exact same behavior
            # as the old loop (each propose sees the progressive index).
            for item in todo:
                _process_one(item, idx)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_process_one, item, idx_snapshot): item["f"]
                           for item in todo}
                for future in as_completed(futures):
                    future.result()  # exceptions are handled inside _process_one
                    if _stop[0]:
                        # Drain already-running futures so their write-lock work
                        # completes cleanly, then cancel the rest.
                        executor.shutdown(wait=True, cancel_futures=True)
                        print("  5 provider errors in a row — provider likely down; stopping (resume later).")
                        break
    finally:
        # Mark chunked raws done ONLY when every slice was durably handled —
        # a chunk that errored (provider/verifier) keeps the whole raw pending so
        # its content is never silently dropped from the backlog.
        for it in chunked_items:
            if len(_chunk_done_map.get(it["chunk_of"], set())) == it["chunk_total"]:
                if synthed.get(it["chunk_of"]) != it["h"]:
                    _mark_done(vault, synthed, synthed_path, lineage,
                               it["chunk_of"], it["h"])
                    _synthed_dirty[0] = True
                # Fix 2: a CHUNKED delta whose slices are ALL durably handled
                # (applied OR rejected in hands-free) is fully seen — advance the
                # cursor to the end so the rejected slices are never re-proposed.
                # Only for deltas (a plain giant session has no lineage page to
                # advance, and non-auto keeps the cursor parked for a human).
                if it.get("chunk_from_delta") and _advance_delta_cursor(
                        lineage, sid=it["sid"], cursor=len(it["body"]),
                        raw_body=it["body"]):
                    _lineage_dirty[0] = True
        # ALWAYS flush the batched state — even if a worker raised. synthed is
        # written only when something was marked (None otherwise); lineage, views
        # and metrics are cheap and idempotent.
        _flush_state(
            vault, synthed,
            synthed_path if _synthed_dirty[0] else None,
            lineage, idx, _metrics)

    errored = _errored[0]
    tail = f"  ({errored} left pending after provider errors — re-run to retry)" if errored else ""
    print(f"\n✓ synth done. {_created[0]} ChangeSet(s) ({_applied[0]} applied, "
          f"{_pending[0]} pending review).{tail}")

    # ── auto-apply pending changesets with route=auto_apply (e.g. code analysis) ──
    if vcfg.get("auto_review", False):
        _apply_pending_auto(vault, outer_lock)

    if _failed_flushes:
        print(f"  WARNING: {len(_failed_flushes)} mark(s) failed to flush to disk "
              f"(ENOSPC/permissions) — see stderr; re-run to retry.")
    try:
        from . import vault as vault_mod
        vault_mod.log_append(vault, f"synth: {len(todo)} raw note(s) processed → "
                                    f"{_created[0]} ChangeSet(s)")
    except Exception:
        pass
    # automatic, non-destructive: surface near-duplicate clusters as a gentle
    # suggestion note in the wiki (the user merges in Obsidian, or ignores it).
    try:
        from . import gardening
        n_sug = gardening.write_suggestions(vault)
        if n_sug:
            print(f"  {n_sug} organization suggestion(s) -> .memex/audit/{gardening.SUGGESTIONS_FILE}")
    except Exception:
        pass
    return 0


def _write_index_md(vault, idx):
    """Regenerate the machine-owned brain catalog + project hubs.

    Legacy public name kept for existing callers and tests; the real
    implementation lives in views.py — generated Markdown now lives under
    .memex/views/, outside the canonical wiki graph."""
    from . import views as views_mod
    views_mod.write_views(vault, idx)


# --------------------------------------------------------------------------- #
# `memex deltas` — read-only dry-run of the session-delta backfill surface.
# --------------------------------------------------------------------------- #
def backfill_report(vault):
    """How the raw corpus would chunk under the delta pipeline. NO LLM, NO writes.

    Groups raws by frontmatter id, orders each session's snapshots by mtime,
    and walks the chain to see which steps are strict appends (prefix-hash
    match) vs non-append (edited/shrunk/reordered). Crossed with the lineage
    checkpoints it reports, per session, how much is already covered
    incrementally vs what a chunked historical backfill would need to re-read.
    """
    raw_dir = canon_mod.raw_dir(vault)
    lineage = _load_lineage(vault)
    by_id: dict[str, list[dict]] = {}
    for f in sorted(raw_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            meta, body = _read_frontmatter(text)
        except OSError:
            continue
        sid = meta.get("id") or f.stem
        by_id.setdefault(sid, []).append({
            "f": f.name, "kind": meta.get("kind", "session"),
            "mtime": f.stat().st_mtime, "chars": len(body), "body": body,
        })
    sessions = []
    for sid, snaps in by_id.items():
        snaps.sort(key=lambda s: s["mtime"])
        steps = []
        for a, b in zip(snaps, snaps[1:]):
            steps.append({
                "append": _is_strict_append(b["body"], a["chars"],
                                            _body_hash(a["body"])),
                "grew": b["chars"] - a["chars"],
            })
        last = snaps[-1]
        # A snapshot is a TRUE duplicate only when its body is a strict prefix
        # of the latest snapshot (fully contained in it). In an edited-down /
        # non-append chain an older snapshot can hold UNIQUE content, so it must
        # not be reported as a safe-to-supersede re-capture.
        true_dups = sum(1 for s in snaps[:-1]
                        if _is_strict_append(last["body"], s["chars"],
                                             _body_hash(s["body"])))
        ck = lineage.get(sid) or {}
        page_edited = None
        if ck.get("chars") and ck.get("slug") and ck.get("page_body_hash"):
            # The lineage records the page body right after its last apply —
            # a mismatch now means a human (or another tool) edited it, so a
            # future delta would merge into hand-curated content.
            page = Path(vault) / "wiki" / f"{ck.get('section', 'topics')}" / f"{ck.get('slug')}.md"
            try:
                page_edited = (page.is_file() and
                               canon_mod.page_body_hash(page.read_text(encoding="utf-8"))
                               != ck.get("page_body_hash"))
            except OSError:
                page_edited = None
        sessions.append({
            "id": sid, "kind": last["kind"],
            "snapshots": len(snaps),
            "first_chars": snaps[0]["chars"],
            "last_chars": last["chars"],
            "append_steps": sum(1 for st in steps if st["append"]),
            "non_append_steps": sum(1 for st in steps if not st["append"]),
            "true_duplicates": true_dups,
            "has_checkpoint": bool(ck.get("chars")),
            "checkpoint_chars": int(ck.get("chars") or 0),
            "page_edited": page_edited,
            "last_raw": last["f"],
        })
    sessions.sort(key=lambda s: -s["last_chars"])
    no_ckpt = [s for s in sessions if not s["has_checkpoint"]]
    return {
        "sessions": sessions,
        "n_sessions": len(sessions),
        "n_files": sum(s["snapshots"] for s in sessions),
        # snapshots fully contained in their session's latest capture (safe to
        # supersede on a backfill); older snapshots of edited-down chains hold
        # unique content and are NOT counted.
        "n_superseded_snapshots": sum(s["true_duplicates"] for s in sessions),
        "total_chars": sum(s["last_chars"] for s in sessions),
        "with_checkpoint": sum(1 for s in sessions if s["has_checkpoint"]),
        "no_checkpoint": len(no_ckpt),
        "no_checkpoint_files": sum(s["snapshots"] for s in no_ckpt),
        "no_checkpoint_chars": sum(s["last_chars"] for s in no_ckpt),
        "page_edited_sessions": sum(1 for s in sessions if s["page_edited"]),
    }


def backfill_run(args) -> int:
    """`memex deltas` — print the dry-run backfill picture for a vault."""
    from . import config as config_mod
    vault = Path(config_mod.resolve_vault(getattr(args, "vault", None)))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1
    r = backfill_report(vault)
    print(f"session-delta backfill dry-run — {vault}")
    print(f"  sessions: {r['n_sessions']}   raw files: {r['n_files']}   "
          f"contained snapshots: {r['n_superseded_snapshots']}")
    print(f"  total chars (latest snapshot): {r['total_chars']:,}")
    print(f"  sessions with wiki checkpoint:  {r['with_checkpoint']}   "
          f"without: {r['no_checkpoint']} "
          f"({r['no_checkpoint_files']} files, {r['no_checkpoint_chars']:,} chars)")
    if r["page_edited_sessions"]:
        print(f"  sessions whose page was edited since its checkpoint: "
              f"{r['page_edited_sessions']}")
    print(f"  {'session id':<40} {'kind':<8} {'snaps':>5} {'append':>7} "
          f"{'edited':>7} {'ckpt':>5} {'last chars':>11}")
    shown = r["sessions"][:30]
    for s in shown:
        print(f"  {s['id'][:40]:<40} {s['kind']:<8} {s['snapshots']:>5} "
              f"{s['append_steps']:>7} {s['non_append_steps']:>7} "
              f"{'yes' if s['has_checkpoint'] else 'no':>5} {s['last_chars']:>11,}")
    if len(r["sessions"]) > len(shown):
        print(f"  ... and {len(r['sessions']) - len(shown)} more sessions")
    print("\n(no writes performed — dry-run only)")
    return 0
