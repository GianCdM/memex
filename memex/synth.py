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

from . import canon as canon_mod
from . import changes as changes_mod
from . import config as config_mod
from . import contracts as ctr
from . import limits as limits_mod
from . import providers
from . import verify as verify_mod
from .format import read_frontmatter as _read_frontmatter  # re-export: helpers moved to format.py

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
- Every durable claim MUST have an exact quote copied from RAW NOTE and a line range.
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
- **Changelog (mandatory):** append exactly ONE line to the `## 📋 Histórico`
  section at the END of the page, summarizing what changed in this merge
  (1-2 sentences max). Format: `- YYYY-MM-DD — summary ([fonte](raw/{raw_fname}))`.
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
- NEVER add durable content the source does not contain: no new facts, numbers, options, profiles, thresholds, or whole sections of substantive material. The source is already the knowledge — you re-file it, you do not enrich it.
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
                "raw": f"raw/{raw_path.name}",
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


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


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
    prev_chars = int(prev.get("chars") or 0)
    if not _is_strict_append(body, prev_chars, prev.get("body_hash")):
        return None
    return body[prev_chars:]


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
        if kind in ("doc", "session") and _delta_target_page(vault, prev) is not None:
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
                prepared.append({
                    "f": f, "h": h, "body": body, "kind": kind,
                    "mode": "delta", "slug": prev["slug"],
                    "section": prev.get("section", "topics"), "delta": delta,
                })
                continue
        prepared.append({"f": f, "h": h, "body": body, "kind": kind,
                         "mode": "full", "slug": None, "delta": None})

    if changed and _synthed_dirty is not None:
        _synthed_dirty[0] = True
    return prepared, lineage


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

    # Source lineage: remembered per source-id as pages are produced, so a future
    # append-only re-capture can delta-merge. Initialized before triage; written
    # at the end of the run (see `_lineage_dirty`).
    lineage = _load_lineage(vault)
    _lineage_dirty = [False]
    _synthed_dirty = [False]
    _triage_log = []
    _metrics = []

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
    print(f"{len(todo)} raw note(s) after triage.\n")

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
        # Two excerpt budgets: the cheap propose classifier only needs enough to
        # route (slug/section/tags) — a coarse decision — while the merge needs
        # the full budget where content fidelity actually lives. For a long
        # session this cuts propose input ~4x with no routing loss.
        propose_excerpt = _excerpt(body, lim.get("raw_propose_chars") or lim["raw_excerpt_chars"])
        merge_excerpt = _excerpt(body, lim["raw_excerpt_chars"])
        raw_lines = raw_full.splitlines()

        # ── phase 1: propose (parallel, readonly; SKIPPED for delta) ──
        if mode == "delta":
            # Append-only re-capture of an already-processed doc: the slug/section
            # come from lineage — no propose call, no index scan.
            prop = {"slug": item["slug"], "section": item.get("section", "topics"),
                    "title": None, "tags": [], "related": [], "distill": None,
                    "claims": []}
            merge_excerpt = _excerpt(item["delta"], lim["raw_excerpt_chars"])
        else:
            try:
                p1 = providers.complete(
                    PROPOSE_PROMPT.format(about=about,
                                          index=_index_summary(idx_at_start, propose_excerpt,
                                                               lim.get("index_neighbors", 0)),
                                          source=source, kind=note_kind, raw=propose_excerpt),
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
                    _synthed_dirty[0] = True
                    print(f"  [{_processed[0]}/{total}] {f.name}: skipped (no durable knowledge)")
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
        # "session-delta". A delta metric carries the tail size (delta_chars)
        # and the pre-raw checkpoint; `checkpoint_after` is added only when the
        # delta actually applies (the lineage write is the real checkpoint).
        if mode == "delta":
            _dmode = f"{note_kind}-delta"
            _dlen = len(item["delta"])
            _ckpt = {"delta_chars": _dlen,
                     "checkpoint_before": len(body) - _dlen}
        else:
            _dmode, _ckpt = "full", {}
        existing_full_pre = page_path_pre.read_text(encoding="utf-8") if page_path_pre.exists() else ""
        _, existing_body_pre = _read_frontmatter(existing_full_pre)

        merge_prompt = ADOPT_MERGE_PROMPT if source == "doc" else DISTILL_MERGE_PROMPT
        merge_kwargs = dict(
            existing=existing_body_pre or "(none yet)", source=source, sid=sid,
            raw=merge_excerpt, raw_fname=f.name,
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
        # wikilinks are only valid against CANONICAL slugs — a link to a page
        # that isn't a current canonical page is unwrapped (kills hallucinations)
        merged_body = _prune_wikilinks(merged_body, canonical_slugs)
        merged_body = _dedup_blocks(merged_body)

        # ── claims + source-relative anchors → absolute raw anchors ──
        claims = []
        ungrounded = False
        for c in (prop.get("claims") or []):
            text = str(c.get("text") or "").strip()
            anchors = []
            for item in (c.get("evidence") or []):
                quote = str(item.get("quote") or "").strip() or text
                anchor = _resolve_anchor(raw_lines, quote, f)
                if anchor:
                    anchors.append(anchor)
            if not anchors:
                anchor = _resolve_anchor(raw_lines, text, f)
                if anchor:
                    anchors.append(anchor)
            if not anchors and note_kind != "doc":
                # Session distillation: a claim that can't be anchored in the
                # raw is ungrounded → parked ambiguous, never a note-* page.
                # Doc adoption is the exception: the ADOPT path preserves a
                # curated document near-verbatim, so body fidelity (verify_
                # fidelity on the source doc) is the gate, not per-claim
                # quote-match. Docs skip the ungrounded flag.
                ungrounded = True
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

        # ── verification + risk (parallel, readonly — the extra complete call) ──
        evidence = verify_mod.validate_evidence(vault, change)
        auto_review = bool(vcfg.get("auto_review", False))
        verify_model = vcfg.get("verify_model") or model_merge

        # Two-stage judging: (1) deterministic gates short-circuit WITHOUT an LLM
        # verify call. An UNGROUNDED session claim (no quote anchor) is ambiguous
        # — checked FIRST, because `validate_evidence` marks an anchor-less claim
        # unsupported, which would otherwise turn a merely-paraphrased claim into
        # a hard reject/discard in auto-review. Only genuinely contradicted
        # evidence (anchored but unsupported) is a deterministic reject; (2) cheap
        # (flash) judge for easy low-risk changes, strong (verify_model) for
        # material ones.
        if not claims and mode != "delta":
            # No extractable claims → there is nothing per-claim to verify. Park
            # (ambiguous) instead of letting the verifier read "[]" source and
            # return unsupported → discard in auto_review. A DELTA is the
            # exception: propose was skipped by design, so it carries no claims —
            # its fidelity is judged by BODY FIDELITY against the appended tail
            # (source_text), not per-claim anchors.
            verification = {"outcome": ctr.Outcome.AMBIGUOUS,
                            "reason": "no extractable claims to verify"}
        elif ungrounded:
            verification = {"outcome": ctr.Outcome.AMBIGUOUS,
                            "reason": "claim text not found in source raw"}
        elif verify_mod.evidence_blocks(evidence):
            verification = {
                "outcome": ctr.Outcome.UNSUPPORTED,
                "reason": "claim evidence not anchored in source",
                "route": ctr.Route.REJECT if auto_review else ctr.Route.ARCHIVE,
            }
        else:
            needs_strong = verify_mod.needs_strong_verify(
                change, lim.get("verify_strong_body_chars", 8000))
            # A delta merge passes the appended TAIL as the source the verifier
            # judges against (it is exactly the content being added), so an
            # append beyond the first N chars of a long doc/session is never
            # verified blind. Bounded to the verify-source cap like a doc.
            v_chars = lim.get("verify_source_chars", 12000)
            v_src = _excerpt(item["delta"], v_chars) if mode == "delta" else None
            if needs_strong:
                # Material content → the STRONG judge (verify_model) decides,
                # capped so a parallel run doesn't fire N strong calls at once.
                if verify_sem is not None:
                    with verify_sem:
                        verification = verify_mod.verify_fidelity(
                            vault, change, kind=kind, model=verify_model,
                            settings=settings, source_text=v_src,
                            source_chars=v_chars)
                else:
                    verification = verify_mod.verify_fidelity(
                        vault, change, kind=kind, model=verify_model,
                        settings=settings, source_text=v_src,
                        source_chars=v_chars)
            else:
                # Easy low-risk change → the cheap (flash) judge alone decides.
                verification = verify_mod.verify_fidelity(
                    vault, change, kind=kind, model=model_propose,
                    settings=settings, source_text=v_src, source_chars=v_chars)

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
                synthed[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                _synthed_dirty[0] = True
                print(f"  [{_processed[0]}/{total}] {f.name} -> auto-rejected "
                      f"({verification.get('reason', verification['route'])})")
                _metrics.append({
                    "fname": f.name, "kind": note_kind, "mode": _dmode,
                    "outcome": verification.get("outcome"),
                    "route": str(verification["route"]),
                    "reason": str(verification.get("reason", ""))[:200],
                    "latency_ms": int((time.time() - _t0) * 1000),
                    "body_chars": len(body),
                    "model_propose": model_propose, "model_merge": model_merge,
                    "verify_model": verify_model, **_ckpt,
                })
                return None
            changes_mod.save_changeset(vault, change)
            _created[0] += 1  # durably saved — applied or parked pending
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
            synthed[f.name] = h
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
            "model_propose": model_propose, "model_merge": model_merge,
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
    raw_dir = Path(vault) / "raw"
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
