"""One-shot migration: replace `tier` with `kind` + `status` in existing vault."""

import json
import sys
from pathlib import Path


def migrate(vault_path):
    vault = Path(vault_path).expanduser().resolve()
    if not (vault / ".memex").exists():
        print(f"error: {vault} is not a memex vault")
        return 1

    idx_path = vault / ".memex" / "index.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        print("no index found — nothing to migrate")
        return 0

    pages = idx.get("pages", [])
    updated = 0

    for p in pages:
        raw_sources = p.get("sources", [])
        kind = "session"
        for src in raw_sources:
            src_str = str(src)
            if src_str.startswith("remember:"):
                kind = "manual"
                break
            elif src_str.startswith("analyze:"):
                kind = "code"
                break
            elif src_str.startswith("doc:"):
                kind = "doc"
                break

        p["kind"] = kind
        p["status"] = "current"
        p.pop("tier", None)

        page_path = vault / "wiki" / p.get("path", "")
        if page_path.exists():
            text = page_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            new_lines = []
            in_fm = False
            fm_done = False
            for i, line in enumerate(lines):
                if i == 0 and line.strip() == "---":
                    in_fm = True
                    new_lines.append(line)
                    continue
                if in_fm and line.strip() == "---":
                    new_lines.append(f"kind: {kind}")
                    new_lines.append("status: current")
                    new_lines.append(line)
                    in_fm = False
                    fm_done = True
                    continue
                if in_fm and line.startswith("tier:"):
                    continue
                new_lines.append(line)
            if fm_done:
                page_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        updated += 1

    idx["pages"] = pages
    idx_path.write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")

    # Map source to proper kind value (old code used 'kind' field with tier values)
    SOURCE_KIND = {
        "claude": "session",
        "session": "session",
        "doc": "doc",
        "remember": "manual",
        "analyze": "code",
        "code": "code",
    }

    from . import canon as canon_mod
    raw_dir = canon_mod.raw_dir(vault)
    raw_migrated = 0
    raw_updated = 0
    for raw_file in raw_dir.glob("*.md"):
        text = raw_file.read_text(encoding="utf-8")
        # Handle old 'tier:' field if present
        if "tier:" in text:
            text = text.replace("tier:", "kind:")
            raw_updated += 1

        # Rewrite 'kind:' value based on source
        lines = text.splitlines()
        source = None
        current_kind = None
        for line in lines:
            if line.startswith("source:"):
                source = line.split(":", 1)[1].strip()
            if line.startswith("kind:"):
                current_kind = line.split(":", 1)[1].strip()

        new_kind = SOURCE_KIND.get(source)
        if new_kind and current_kind != new_kind:
            text = text.replace(f"kind: {current_kind}", f"kind: {new_kind}")
            raw_file.write_text(text, encoding="utf-8")
            raw_migrated += 1

    print(f"✓ migrated {updated} wiki pages")
    print(f"✓ migrated {raw_migrated} raw notes (kind values)")
    print(f"✓ fixed {raw_updated} raw notes (tier→kind field)")
    return 0


if __name__ == "__main__":
    vault = sys.argv[1] if len(sys.argv) > 1 else None
    if not vault:
        print("usage: python -m memex.migrate_kind <vault-path>")
        sys.exit(1)
    sys.exit(migrate(vault))
