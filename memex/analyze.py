"""memex analyze — synthesize a codebase into a FEW architecture pages.

Code is a graph, not a narrative: one page per file is an anti-pattern (it just
recreates the repo in Markdown). analyze builds a BOUNDED digest of the repo
(tree, languages, manifests, READMEs) and asks the LLM for C4-style pages:
one "Architecture Overview" + a page per significant top-level module. Pages are
gold tier, written straight to wiki/ (no per-file raw). Re-running overwrites
them (snapshotting the previous version to .memex/history/ first).

This is the CODE pipeline of the 4 routing rules:
  sessions -> distill | docs -> adopt | CODE -> analyze | config -> skip
Config/data files are read only to understand the stack; they never become pages.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import config as config_mod
from . import providers
from . import synth

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
             "__pycache__", ".next", "target", ".idea", ".vscode", ".mypy_cache",
             ".pytest_cache", "vendor", ".gradle", "coverage", ".turbo"}
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
            ".scala", ".rb", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".php", ".sql"}
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
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True, timeout=30)
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
        return p.read_text(errors="ignore")[:limit]
    except Exception:
        return ""


def _digest(root, files):
    exts = Counter(Path(f).suffix.lower() for f in files)
    langs = ", ".join(f"{e or '(none)'}:{c}" for e, c in exts.most_common(12))
    topdirs = Counter(
        (Path(f).parts[0] if len(Path(f).parts) > 1 else "(root)") for f in files)
    tree = "\n".join(f"  {d}/  ({c} files)" for d, c in topdirs.most_common(25))
    keyfiles = []
    for name in KEY_FILES:
        txt = _read(root / name, 1800)
        if txt.strip():
            keyfiles.append(f"### {name}\n{txt}")
    digest = (
        f"REPO: {root.name}\nTRACKED FILES: {len(files)}\n"
        f"LANGUAGES (ext:count): {langs}\n\n"
        f"TOP-LEVEL STRUCTURE:\n{tree}\n\n"
        f"KEY FILES (manifests / READMEs):\n" + "\n\n".join(keyfiles)
    )
    return digest[:9000]


def _modules(root, files, n):
    """Top-N top-level dirs that hold real code, by code-file count."""
    bydir = defaultdict(list)
    for f in files:
        parts = Path(f).parts
        if len(parts) > 1 and Path(f).suffix.lower() in CODE_EXT:
            bydir[parts[0]].append(f)
    ranked = sorted(bydir.items(), key=lambda kv: len(kv[1]), reverse=True)
    return ranked[:n]


def _module_digest(root, dirname, mfiles):
    listing = "\n".join(f"  {f}" for f in sorted(mfiles)[:60])
    readme = _read(root / dirname / "README.md", 1500) or _read(root / dirname / "readme.md", 1500)
    out = f"MODULE: {dirname}/  ({len(mfiles)} code files)\n\nFILES:\n{listing}\n"
    if readme.strip():
        out += f"\nMODULE README:\n{readme}"
    return out[:6000]


def _write_pages(vault, root, pages):
    """Write architecture pages (gold), snapshotting + updating index/changelog."""
    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text())
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
            (hist / f"{int(time.time())}.md").write_text(page_path.read_text())
        page_path.parent.mkdir(parents=True, exist_ok=True)
        tags = ["architecture", repo_tag]
        page_path.write_text(synth._render_page(
            title=title, tags=tags, tier="gold", sources=[src], body=body))
        by_slug[slug] = {
            "slug": slug, "title": title, "section": "topics", "tier": "gold",
            "tags": tags, "sources": [src], "summary": (body or "")[:200],
            "path": str(page_path.relative_to(vault / "wiki")),
        }
        with changelog.open("a") as ch:
            ch.write(json.dumps({
                "ts": int(time.time()), "page": slug, "tier": "gold",
                "action": "update" if existed else "create",
                "source": src, "raw": "analyze"}) + "\n")
    idx["pages"] = list(by_slug.values())
    idx_path.write_text(json.dumps(idx, indent=2) + "\n")
    synth._write_index_md(vault, idx)


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

    vcfg = config_mod.load_vault(vault)
    name, kind, settings = config_mod.resolve_provider(
        getattr(args, "provider", None), vault_cfg=vcfg)
    model = getattr(args, "model_merge", None) or settings.get("model_merge")
    if not model:
        print(f"error: no merge model for provider '{name}'. run `memex doctor`.")
        return 1

    files = _repo_files(root)
    if not files:
        print(f"no tracked files found in {root}.")
        return 1

    print(f"analyze: {root.name} ({len(files)} files)  provider={name}/{model}")
    repo_slug = synth._kebab(root.name)
    pages = []

    # 1) architecture overview (one page)
    try:
        body = synth._clean_body(providers.complete(
            ARCH_OVERVIEW_PROMPT.format(digest=_digest(root, files)),
            kind=kind, model=model, settings=settings))
    except providers.ProviderError as e:
        print(f"  overview: provider error: {e}")
        return 2
    pages.append((f"{repo_slug}-architecture", f"{root.name} — Arquitetura", body))
    print(f"  + overview -> {repo_slug}-architecture")

    # 2) one page per significant top-level module (bounded)
    limit = getattr(args, "modules", None)
    nmod = 0 if limit == 0 else (limit or 6)
    for dirname, mfiles in _modules(root, files, nmod):
        try:
            mbody = synth._clean_body(providers.complete(
                ARCH_MODULE_PROMPT.format(digest=_module_digest(root, dirname, mfiles)),
                kind=kind, model=model, settings=settings))
        except providers.ProviderError as e:
            print(f"  module {dirname}: provider error: {e} — skipped")
            continue
        pages.append((f"{repo_slug}-{synth._kebab(dirname)}", f"{root.name}/{dirname}", mbody))
        print(f"  + module {dirname} -> {repo_slug}-{synth._kebab(dirname)}")

    _write_pages(vault, root, pages)
    print(f"\n✓ analyze done. {len(pages)} architecture page(s) (gold) in the wiki.")
    return 0
