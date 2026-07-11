"""now/ — WORKING MEMORY: one handoff page per project ("where we left off").

Two writers, one page (`<vault>/now/<project>.md`):
- `memex handoff --stdin`  — the agent (or you) saves state DELIBERATELY,
  mid-session, in its own words. LLM-free, instant, highest quality (the model
  in the session knows the state better than any after-the-fact summarizer).
- auto-generation on reflect — after a session ends, a cheap/fast model distills
  the transcript tail into the same page, as a fallback when nobody saved state.

A fresh handoff holds off auto-generation for a while (`now_handoff_hold_hours`)
so the deliberate save isn't clobbered by the automatic one for the same session.

The page is SHORT-TERM by design: current state only, overwritten freely.
Durable knowledge graduates to wiki/ via synth — this file is the bridge
between one session and the next, not an archive.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import limits as limits_mod
from . import providers
from . import vault as vault_mod

NOW_PROMPT = """You write the WORKING-MEMORY handoff page for a coding project — the page a fresh
AI session reads FIRST to continue exactly where the previous session left off.

From the RAW session transcript below, output ONLY a Markdown body with EXACTLY these
sections (keep the content's own language — Portuguese/English as written):

## Contexto
1-3 sentences: what is being worked on and why.

## Estado atual
What was done / decided in THIS session; where things stand right now. Be specific.

## Próximos passos
The concrete next actions, most important first, as a `- [ ]` checklist.

## Arquivos-chave
Bullets: the file paths / wiki pages that matter right now, each with a one-line why.

## Threads abertos
Unresolved questions, blockers, anything waiting on someone. OMIT this section if none.

Rules:
- This is SHORT-TERM memory: the CURRENT state only — no history lessons, no chat
  transcription, no praise/filler.
- The END of the transcript is the freshest state; weight it accordingly.
- <= 60 lines total. No preamble, no frontmatter, no H1 — start at "## Contexto".

RAW SESSION (project={project}; tail of the conversation):
{raw}
"""

_KEBAB_RE = re.compile(r"[^a-z0-9]+")


def _kebab(s):
    return _KEBAB_RE.sub("-", (s or "").lower()).strip("-")


_PROJECT_CACHE = {}


def project_key(cwd):
    """The 'project' a page belongs to: the nearest git repo containing `cwd`
    (its dir name), else `cwd`'s basename. The single key that ties sessions,
    docs, architecture pages, the project hub and the now-page together."""
    if not cwd:
        return None
    cwd = str(cwd)
    if cwd in _PROJECT_CACHE:
        return _PROJECT_CACHE[cwd]
    proj = None
    try:
        path = Path(cwd)
        for d in [path, *path.parents]:
            if (d / ".git").exists():
                proj = _kebab(d.name)
                break
        if not proj:
            proj = _kebab(path.name) or None
    except Exception:
        proj = None
    _PROJECT_CACHE[cwd] = proj
    return proj


def now_path(vault, project) -> Path:
    return Path(vault) / "now" / f"{project}.md"


def read_now(vault, project):
    """(meta, body) of the project's now-page, or (None, None)."""
    p = now_path(vault, project)
    if not p.is_file():
        return None, None
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    return _split_frontmatter(text)


def _split_frontmatter(text):
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


def write_now(vault, project, body, *, author, session_id=None) -> Path:
    """Overwrite the project's now-page (short-term memory is rewritten, not
    accumulated). Appends one line to the vault log."""
    vault = Path(vault)
    body = (body or "").strip()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = (
        "---\n"
        f"project: {project}\n"
        f"updated: {ts}\n"
        f"author: {author}\n"
        + (f"session: {session_id}\n" if session_id else "")
        + "---\n\n"
    )
    p = now_path(vault, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm + body + "\n", encoding="utf-8")
    vault_mod.log_append(vault, f"now/{project} updated ({author})")
    return p


def hold_active(vault, project, hold_hours) -> bool:
    """True when a deliberate handoff is fresh enough that auto-generation
    should stand down instead of clobbering it."""
    meta, _ = read_now(vault, project)
    if not meta or meta.get("author") != "handoff":
        return False
    try:
        updated = datetime.strptime(meta.get("updated", ""), "%Y-%m-%dT%H:%M:%SZ")
        age_h = (datetime.utcnow() - updated).total_seconds() / 3600.0
        return age_h < float(hold_hours)
    except Exception:
        return False


def generate(vault, project, raw_text, *, provider=None) -> str:
    """Distill a transcript tail into the handoff body via the cheap/fast model.
    Raises providers.ProviderError on failure (caller decides how to degrade)."""
    lim = limits_mod.load(vault)
    vcfg = config_mod.load_vault(Path(vault))
    name, kind, settings = config_mod.resolve_provider(provider, vault_cfg=vcfg)
    model = settings.get("model_propose") or settings.get("model_merge")
    if not model:
        raise providers.ProviderError(f"no model configured for provider '{name}'")
    tail = (raw_text or "")[-lim["now_source_chars"]:]
    body = providers.complete(
        NOW_PROMPT.format(project=project, raw=tail),
        kind=kind, model=model, settings=settings)
    body = _sanitize_body(body, lim["now_max_chars"])
    if not body:
        raise providers.ProviderError("empty now-page body from provider")
    return body


def _sanitize_body(body, max_chars):
    """Model output → clean body: strip fences/frontmatter/preamble, cap size."""
    body = (body or "").strip()
    body = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", body).strip()
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:].lstrip("\n")
    i = body.find("## ")
    if i > 0:
        body = body[i:]  # drop any preamble before the first section
    return body[:max_chars].strip()


def handoff_cmd(args) -> int:
    """`memex handoff` — deliberate, LLM-free state save (or --show to read)."""
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` first).")
        return 1
    project = getattr(args, "project", None) or project_key(Path.cwd()) or "workspace"

    if getattr(args, "show", False):
        meta, body = read_now(vault, project)
        if not body:
            print(f"no now-page yet for project '{project}' ({now_path(vault, project)})")
            return 0
        print(f"# now/{project}.md  (updated {meta.get('updated', '?')}, {meta.get('author', '?')})\n")
        print(body)
        return 0

    body = getattr(args, "text", None)
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()
    if not body or not body.strip():
        print('usage: memex handoff --stdin  (pipe the Markdown handoff)  |  --text "..."  |  --show')
        return 1
    p = write_now(vault, project, body.strip(), author="handoff")
    print(f"✓ working memory saved: {p}")
    print("  (the next session in this project will boot with it)")
    return 0
