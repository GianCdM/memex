"""memex analyze — synthesize a codebase into architecture pages.

Code is a graph, not a narrative: one page per file is an anti-pattern (it just
recreates the repo in Markdown). analyze builds a BOUNDED digest of the repo and
asks the LLM for C4-style pages — one "Architecture Overview" + one page per
SIGNIFICANT module.

It SCALES with the repo: a small lib → a couple of pages; a big monorepo →
dozens/hundreds of module pages (one per package/module, descending into
src/packages/… containers), but NEVER a page per file — each page is a
module-level synthesis. Pages are gold tier, written straight to wiki/.

Every knob lives in memex/limits.py (override per-vault via config.json "limits").
The CODE rule of 4: sessions=distill | docs=adopt | CODE=analyze | config=skip.
Config/data files are read only to understand the stack; they never become pages.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import config as config_mod
from . import limits as limits_mod
from . import providers
from . import synth

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
             "__pycache__", ".next", "target", ".idea", ".vscode", ".mypy_cache",
             ".pytest_cache", "vendor", ".gradle", "coverage", ".turbo"}

# Summary displayed in project hubs and index.md — how many chars we allow.
# Should fit a hub line + preserve full sentences (see _extract_summary).
_SUMMARY_MAX_CHARS = 280


def _extract_summary(body: str, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """Pull a hub-safe one-liner from a page body.

    We want the first complete sentence(s) of the FIRST prose paragraph —
    never a hard cut mid-word like the old `body[:200]` did. Skips any leading
    Markdown structure (headings, blockquotes, bullets) so pages that start
    with `# Title`, `## Visão geral`, `> quote` or `- bullet` still yield a
    readable one-liner. Falls back gracefully when the body is empty or
    non-prose.
    """
    if not body:
        return ""
    prose = ""
    for para in body.split("\n\n"):
        # Strip leading Markdown structure from each line of this paragraph,
        # then keep only lines that read as prose (not empty, not still a
        # heading/list/quote marker after stripping).
        lines = []
        for ln in para.splitlines():
            s = ln.lstrip()
            # Drop headings, blockquotes, bullets, task-list markers.
            if s.startswith(("#", ">", "- ", "* ", "+ ")):
                continue
            if s and s[0].isdigit() and s.lstrip("0123456789").startswith((". ", ") ")):
                continue  # "1. item" numbered list
            if s:
                lines.append(s)
        if lines:
            prose = " ".join(lines).strip()
            break
    if not prose:
        return ""
    # Collapse whitespace so we don't ship raw markdown newlines to the hub.
    flat = " ".join(prose.split())
    if len(flat) <= max_chars:
        return flat
    # Take whole sentences until we hit the char budget — never cut a word.
    # Sentence boundary = ". " / "! " / "? " (skips "..." / abbreviations).
    out = []
    total = 0
    buf = ""
    for ch in flat:
        buf += ch
        if ch in ".!?" and (len(buf) < 2 or buf[-2] != "."):
            # end of a sentence — commit if it still fits
            if total + len(buf) > max_chars:
                break
            out.append(buf.strip())
            total += len(buf)
            buf = ""
    if out:
        return " ".join(out)
    # Single sentence longer than the budget — cut at the last space so we
    # never leave a mangled word, and mark it as an ellipsis so the reader
    # knows it's a preview.
    cut = flat[:max_chars].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
            ".scala", ".rb", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".php", ".sql"}
# monorepo "container" dirs we descend INTO so each package becomes its own module
CONTAINER_DIRS = {"src", "packages", "apps", "libs", "lib", "services", "internal",
                  "pkg", "modules", "components", "cmd", "crates", "projects"}
# manifests/READMEs we read to understand the stack (NOT turned into pages)
KEY_FILES = ["README.md", "README.rst", "readme.md", "pyproject.toml", "package.json",
             "go.mod", "Cargo.toml", "build.gradle", "build.gradle.kts", "pom.xml",
             "requirements.txt", "setup.py", "setup.cfg", "Makefile", "Dockerfile",
             "docker-compose.yml", "docker-compose.yaml", "tsconfig.json"]

ARCH_OVERVIEW_PROMPT = """You are documenting the ARCHITECTURE of a codebase for a knowledge wiki. From the DIGEST below, write ONE concise "Architecture Overview" page. Output ONLY the Markdown body — NO YAML frontmatter, NO H1 title, start directly at a `## ` heading. NO preamble.

Cover these (SKIP any section the digest doesn't support — do NOT invent):
- ## Visão geral — what the system is and does, in 2-4 sentences
- ## Stack — main languages, frameworks, build/deploy tooling (from the manifests)
- ## Estrutura — the top-level modules/dirs and what each is for
- ## Pontos de entrada — how it runs / entry points
- ## Fluxos principais — notable flows, ONLY if clearly inferable

Be factual and grounded ONLY in the digest — never guess at code you cannot see. Keep the repo's own language where evident; otherwise Portuguese.

DIGEST:
{digest}
"""

ARCH_MODULE_PROMPT = """You are documenting ONE module of a codebase for an architecture wiki. From the MODULE DIGEST, write a concise page about THIS module only. Output ONLY the Markdown body — NO YAML frontmatter, NO H1 title, start directly at a `## ` heading. NO preamble.

Describe: what this module is responsible for, its key files/submodules, and how it relates to the rest of the system — grounded ONLY in the digest, no invention. Keep it short: a couple of paragraphs + a bullet list of the key files. Keep the repo's own language where evident; otherwise Portuguese.

MODULE DIGEST:
{digest}
"""


def _repo_files(root):
    """Relative paths of tracked files (git ls-files), falling back to a walk."""
    from . import proc
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"],
                             **proc.run_kwargs(capture_output=True, text=True, timeout=30))
        if out.returncode == 0 and out.stdout.strip():
            files = [l for l in out.stdout.splitlines() if l.strip()]
            return [f for f in files if not any(d in Path(f).parts for d in SKIP_DIRS)]
    except Exception:
        pass
    files = []
    for p in root.rglob("*"):
        if p.is_file() and not any(d in p.parts for d in SKIP_DIRS):
            files.append(str(p.relative_to(root)))
    return files


def _read(p, limit):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def _digest(root, files, lim):
    exts = Counter(Path(f).suffix.lower() for f in files)
    langs = ", ".join(f"{e or '(none)'}:{c}" for e, c in exts.most_common(12))
    topdirs = Counter(
        (Path(f).parts[0] if len(Path(f).parts) > 1 else "(root)") for f in files)
    tree = "\n".join(f"  {d}/  ({c} files)"
                     for d, c in topdirs.most_common(lim["analyze_tree_dirs"]))
    keyfiles = []
    for name in KEY_FILES:
        txt = _read(root / name, lim["analyze_keyfile_chars"])
        if txt.strip():
            keyfiles.append(f"### {name}\n{txt}")
    digest = (
        f"REPO: {root.name}\nTRACKED FILES: {len(files)}\n"
        f"LANGUAGES (ext:count): {langs}\n\n"
        f"TOP-LEVEL STRUCTURE:\n{tree}\n\n"
        f"KEY FILES (manifests / READMEs):\n" + "\n\n".join(keyfiles)
    )
    return digest[:lim["analyze_overview_chars"]]


def _module_key(relpath, max_depth):
    """The 'module' a file belongs to: its top dir, descending through monorepo
    containers (src/packages/…) up to max_depth. None for root-level files."""
    parts = Path(relpath).parts
    if len(parts) < 2:
        return None
    depth = 1
    while depth < max_depth and depth < len(parts) - 1 and parts[depth - 1] in CONTAINER_DIRS:
        depth += 1
    return "/".join(parts[:depth])


def _modules(root, files, lim):
    """Significant modules to document (one page each, NEVER per file).
    Returns (modules_capped, total_qualified) so the caller can flag truncation."""
    groups = defaultdict(list)
    for f in files:
        if Path(f).suffix.lower() not in CODE_EXT:
            continue
        key = _module_key(f, lim["analyze_module_depth"])
        if key:
            groups[key].append(f)
    qualified = [(k, v) for k, v in groups.items()
                 if len(v) >= lim["analyze_module_min_files"]]
    qualified.sort(key=lambda kv: len(kv[1]), reverse=True)
    cap = lim["analyze_max_module_pages"]
    return qualified[:cap], len(qualified)


def _module_digest(root, modkey, mfiles, lim):
    listing = "\n".join(f"  {f}" for f in sorted(mfiles)[:lim["analyze_files_per_module"]])
    readme = (_read(root / modkey / "README.md", lim["analyze_keyfile_chars"])
              or _read(root / modkey / "readme.md", lim["analyze_keyfile_chars"]))
    out = f"MODULE: {modkey}/  ({len(mfiles)} code files)\n\nFILES:\n{listing}\n"
    if readme.strip():
        out += f"\nMODULE README:\n{readme}"
    return out[:lim["analyze_module_chars"]]


def _write_pages(vault, root, pages):
    """Write architecture pages (gold), snapshotting + updating index/changelog."""
    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        idx = {"pages": []}
    by_slug = {p["slug"]: p for p in idx.get("pages", [])}
    changelog = vault / ".memex" / "changelog.jsonl"
    src = f"analyze:{root.name}"
    repo_tag = synth._kebab(root.name)
    for slug, title, body in pages:
        page_path = vault / "wiki" / "topics" / f"{slug}.md"
        existed = page_path.exists()
        if existed:  # gold: snapshot before overwrite (audit / revert)
            hist = vault / ".memex" / "history" / slug
            hist.mkdir(parents=True, exist_ok=True)
            (hist / f"{int(time.time())}.md").write_text(
                page_path.read_text(encoding="utf-8"), encoding="utf-8")
        page_path.parent.mkdir(parents=True, exist_ok=True)
        tags = ["architecture", repo_tag]
        page_path.write_text(synth._render_page(
            title=title, tags=tags, tier="gold", sources=[src], body=body,
            project=repo_tag), encoding="utf-8")
        by_slug[slug] = {
            "slug": slug, "title": title, "section": "topics", "tier": "gold",
            "tags": tags, "sources": [src], "project": repo_tag,
            "summary": _extract_summary(body or ""),
            "path": str(page_path.relative_to(vault / "wiki")),
        }
        with changelog.open("a", encoding="utf-8") as ch:
            ch.write(json.dumps({
                "ts": int(time.time()), "page": slug, "tier": "gold",
                "action": "update" if existed else "create",
                "source": src, "raw": "analyze"}) + "\n")
    idx["pages"] = list(by_slug.values())
    idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    synth._write_index_md(vault, idx)


def _discover_repos(root):
    """Repos to analyze. If `root` is itself a git repo → just [root]. Otherwise
    (a workspace folder holding several repos) find the nested git repos under it, so
    each is analyzed on its own — and each respects ITS OWN .gitignore (git ls-files
    runs inside each repo). Falls back to [root] if nothing nested is found."""
    if (root / ".git").exists():
        return [root]
    repos = set()
    for pattern in ("*/.git", "*/*/.git"):
        for git in root.glob(pattern):
            if git.parent.is_dir():
                repos.add(git.parent.resolve())
    return sorted(repos) if repos else [root]


def _analyze_repo(repo, kind, model, settings, lim, max_modules):
    """Analyze ONE repo → (pages, qualified_module_count). Makes the LLM calls."""
    files = _repo_files(repo)
    if not files:
        print(f"  - {repo.name}: no analyzable code files — skipped")
        return [], 0
    repo_slug = synth._kebab(repo.name)
    pages = []
    try:
        body = synth._clean_body(providers.complete(
            ARCH_OVERVIEW_PROMPT.format(digest=_digest(repo, files, lim)),
            kind=kind, model=model, settings=settings))
    except providers.ProviderError as e:
        print(f"  {repo.name}: overview error: {e} — skipped")
        return [], 0
    pages.append((f"{repo_slug}-architecture", f"{repo.name} — Arquitetura", body))
    print(f"  + {repo.name}: overview")
    qualified = 0
    if max_modules > 0:
        from . import ui
        lim_m = dict(lim)
        lim_m["analyze_max_module_pages"] = max_modules
        mods, qualified = _modules(repo, files, lim_m)
        with ui.Progress(f"      {repo.name} modules", total=len(mods)) as bar:
            for modkey, mfiles in mods:
                bar.update(suffix=modkey[:32])
                try:
                    mbody = synth._clean_body(providers.complete(
                        ARCH_MODULE_PROMPT.format(digest=_module_digest(repo, modkey, mfiles, lim)),
                        kind=kind, model=model, settings=settings))
                except providers.ProviderError as e:
                    if not bar.enabled:
                        print(f"      {repo.name}/{modkey}: {e} — skipped")
                    continue
                pages.append((f"{repo_slug}-{synth._kebab(modkey)}", f"{repo.name}/{modkey}", mbody))
                if not bar.enabled:
                    print(f"      + module {modkey} ({len(mfiles)} files)")
        print(f"      modules: {len(pages) - 1} page(s)")
    return pages, qualified


def run(args) -> int:
    root = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    vault_arg = getattr(args, "vault", None)
    if not vault_arg:
        vault_arg = config_mod.load_global().get("default_vault")
        if not vault_arg:
            print("error: no --vault and no default vault. run `memex init` first, or pass --vault.")
            return 1
    vault = Path(vault_arg).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault (run `memex init` / `memex vault new`).")
        return 1
    if not root.exists():
        print(f"error: repo not found: {root}")
        return 1

    lim = limits_mod.load(vault)
    vcfg = config_mod.load_vault(vault)
    name, kind, settings = config_mod.resolve_provider(
        getattr(args, "provider", None), vault_cfg=vcfg)
    model = getattr(args, "model_merge", None) or settings.get("model_merge")
    if not model:
        print(f"error: no merge model for provider '{name}'. run `memex doctor`.")
        return 1

    repos = _discover_repos(root)
    multi = len(repos) > 1
    # module policy: an explicit --modules always wins; otherwise overview-only for
    # a multi-repo workspace (cheap first pass), full module scaling for a single repo.
    if getattr(args, "modules", None) is not None:
        max_modules = max(0, args.modules)
    else:
        max_modules = 0 if multi else lim["analyze_max_module_pages"]

    print(f"analyze: provider={name}/{model}")
    if multi:
        scope = "overview only" if max_modules == 0 else f"overview + up to {max_modules} modules each"
        print(f"  {root.name}/ holds {len(repos)} repos — analyzing each ({scope})")

    total_pages = 0
    for repo in repos:
        pages, qualified = _analyze_repo(repo, kind, model, settings, lim, max_modules)
        if not pages:
            continue
        _write_pages(vault, repo, pages)
        total_pages += len(pages)
        written_mods = len(pages) - 1
        if qualified > written_mods:  # never silently truncate
            print(f"    note: {repo.name} has {qualified} modules; wrote {written_mods} "
                  f"(pass --modules / raise analyze_max_module_pages for more)")

    print(f"\n✓ analyze done. {total_pages} architecture page(s) across {len(repos)} repo(s).")
    return 0
