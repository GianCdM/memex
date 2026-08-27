"""memex embed — precompute embeddings for every wiki page (optional).

Semantic recall is off by default (`memex` is stdlib-only and works fine with
lexical Jaccard/IDF). When you turn it on by configuring `provider.embeddings`
in ~/.config/memex/config.json, this command sweeps the vault, calls the
configured OpenAI-compatible /embeddings endpoint in batches, and writes one
JSONL file per section under `.memex/embeddings/`:

    .memex/embeddings/
        topics.jsonl      # {slug, dim, vec (list[float])} per page
        entities.jsonl
        decisions.jsonl
        _meta.json        # {model, dim, generated_at, count}

The file is compact JSON on purpose — no numpy dependency, one page per line so
appends and dedup are trivial. Vectors are L2-normalized so recall can score
with a plain dot product (no sqrt).

Idempotent: pages whose content hash is unchanged are skipped. Rerun any time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

from . import canon as canon_mod
from . import config as config_mod
from . import providers
from . import synth


BATCH_SIZE = 40          # ~20k tokens/batch — fits GenPlat's 30k token/min window
EMBED_INPUT_CHARS = 2000  # per-page char budget sent to the model


def _page_text(vault: Path, p: dict) -> str:
    """Compact string used to embed a page: title + tags + summary + top of the
    body. Bounded by EMBED_INPUT_CHARS so a huge page doesn't blow the request.
    """
    parts = [
        p.get("title") or p["slug"],
        " ".join(p.get("tags") or []),
        p.get("summary") or "",
    ]
    fp = vault / "wiki" / (p.get("path") or "")
    body = ""
    if fp.exists():
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        _, body = synth._read_frontmatter(raw)
    text = "\n".join(x for x in parts if x) + "\n" + (body or "")
    return text[:EMBED_INPUT_CHARS]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize so dot product equals cosine similarity. Cheap and lets the
    recall hot path skip a sqrt on every comparison."""
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _load_existing(section_file: Path) -> dict[str, dict]:
    """Existing embeddings for a section, indexed by slug. Silent on I/O errors
    (we'll just re-embed everything)."""
    out = {}
    if not section_file.exists():
        return out
    try:
        for line in section_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            out[rec["slug"]] = rec
    except Exception:
        return {}
    return out


def _write_section(section_file: Path, records: list[dict]) -> None:
    section_file.parent.mkdir(parents=True, exist_ok=True)
    with section_file.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault.")
        return 1

    vcfg = config_mod.load_vault(vault)
    model, settings = config_mod.resolve_embeddings(vault_cfg=vcfg)
    if not model:
        print("error: no embeddings provider configured.")
        print("  set one via `memex config set embeddings.base_url ...` and")
        print("  `memex config set embeddings.model ...` (e.g. text-embedding-3-small).")
        return 1

    # input_type is sent only when configured; many providers (OpenAI, Voyage)
    # ignore it, others (Nvidia, Cohere) require a specific value.
    idx_settings = dict(settings)

    force = getattr(args, "force", False)
    dry_run = getattr(args, "dry_run", False)

    try:
        idx = json.loads((vault / ".memex" / "index.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: can't read index: {e}")
        return 1

    pages = canon_mod.canonical_pages(vault, idx)
    if not pages:
        print("empty brain — nothing to embed.")
        return 0

    embed_dir = vault / ".memex" / "embeddings"
    embed_dir.mkdir(parents=True, exist_ok=True)

    # Group pages by section so files stay reasonable and correspond to the
    # wiki tree structure (topics/entities/decisions).
    by_section: dict[str, list[dict]] = {}
    for p in pages:
        by_section.setdefault(p.get("section", "topics"), []).append(p)

    total_embedded = 0
    total_skipped = 0
    total_pages = len(pages)
    print(f"vault: {vault}")
    print(f"  {total_pages} page(s) across {len(by_section)} section(s)")
    print(f"  model: {model}")
    if dry_run:
        print("  (dry-run — nothing will be written)")

    started = time.time()
    for section, spages in sorted(by_section.items()):
        section_file = embed_dir / f"{section}.jsonl"
        existing = {} if force else _load_existing(section_file)
        # Compute what to embed: pages whose content hash differs from stored.
        to_embed = []
        current_records: dict[str, dict] = {}
        for p in spages:
            text = _page_text(vault, p)
            h = _hash(text)
            prior = existing.get(p["slug"])
            if prior and prior.get("hash") == h and prior.get("model") == model:
                current_records[p["slug"]] = prior
                total_skipped += 1
            else:
                to_embed.append({"slug": p["slug"], "text": text, "hash": h})

        if not to_embed:
            _write_section(section_file, list(current_records.values()))
            print(f"  {section}: {len(spages)} pages, all up to date")
            continue

        print(f"  {section}: embedding {len(to_embed)} / {len(spages)} pages "
              f"({len(spages) - len(to_embed)} cached)...")
        if dry_run:
            continue

        # Batch the API calls to stay under provider limits and to amortize
        # round-trip latency. `embed()` returns vectors in input order.
        for i in range(0, len(to_embed), BATCH_SIZE):
            batch = to_embed[i:i + BATCH_SIZE]
            inputs = [b["text"] for b in batch]
            try:
                vecs = providers.embed(inputs, model=model, settings=idx_settings)
            except providers.ProviderError as e:
                print(f"    error at batch {i//BATCH_SIZE + 1}: {e}")
                # partial progress: what we already computed this run is written
                # below outside the loop — but only if we have something. Bail
                # so the user can retry after fixing config.
                if current_records:
                    _write_section(section_file, list(current_records.values()))
                return 2
            for b, v in zip(batch, vecs):
                v = _normalize(v)
                current_records[b["slug"]] = {
                    "slug": b["slug"],
                    "hash": b["hash"],
                    "model": model,
                    "dim": len(v),
                    "vec": v,
                }
                total_embedded += 1

            print(f"    batch {i//BATCH_SIZE + 1}: +{len(batch)} vectors "
                  f"({total_embedded} total)")

        _write_section(section_file, list(current_records.values()))

    if dry_run:
        print("\n(dry-run — nothing written)")
        return 0

    # Meta pointer so recall knows what model produced these vectors.
    dims = set()
    for section_file in embed_dir.glob("*.jsonl"):
        for line in section_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    dims.add(json.loads(line)["dim"])
                except Exception:
                    pass
    (embed_dir / "_meta.json").write_text(json.dumps({
        "model": model,
        "dim": next(iter(dims)) if len(dims) == 1 else list(dims),
        "generated_at": int(time.time()),
        "count": total_embedded + total_skipped,
    }, indent=2) + "\n", encoding="utf-8")

    elapsed = time.time() - started
    print(f"\n✓ embed done. {total_embedded} embedded, {total_skipped} cached "
          f"({total_pages} total) in {elapsed:.1f}s.")
    return 0
