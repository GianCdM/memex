"""memex review — inspect and act on the ChangeSet review queue.

The review lifecycle is deliberately small: list what is parked in
`.memex/review/pending/`, show one ChangeSet's full JSON, then approve (apply
with explicit approval), reject (move to `rejected` with an optional reason), or
rollback (reverse an applied ChangeSet). All state movement is recoverable and
journaled by `changes`.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import changes as changes_mod
from . import config as config_mod


def list_changesets(vault: Path, state: str = "pending") -> list[dict]:
    """Summaries (id, operation, classification slug, risk, reason) of the
    ChangeSets parked under `.memex/review/<state>/`."""
    review_dir = Path(vault) / ".memex" / "review" / state
    out = []
    if review_dir.is_dir():
        for fp in sorted(review_dir.glob("*.json")):
            change = json.loads(fp.read_text(encoding="utf-8"))
            out.append({
                "id": change.get("id"),
                "operation": change.get("operation"),
                "slug": (change.get("classification") or {}).get("slug")
                or (change.get("target") or {}).get("slug"),
                "risk": change.get("risk"),
                "reason": change.get("reason"),
            })
    return out


def run(args) -> int:
    vault = config_mod.resolve_vault(getattr(args, "vault", None))
    action = getattr(args, "action", "list")

    if action == "list":
        for item in list_changesets(vault):
            line = f"{item['id']}  {item['operation']}  {item['slug']}  risk={item['risk']}"
            if item.get("reason"):
                line += f"  reason={item['reason']}"
            print(line)
        return 0

    if action == "show":
        try:
            change, _ = changes_mod.load_changeset(vault, args.change_id)
        except FileNotFoundError as exc:
            print(f"error: {exc}")
            return 1
        print(json.dumps(change, indent=2, ensure_ascii=False))
        return 0

    if action == "approve":
        result = changes_mod.apply_changeset(vault, args.change_id, approved=True)
        print(_state_line(result))
        return 0

    if action == "reject":
        change = changes_mod.transition_changeset(
            vault, args.change_id, "rejected", reason=getattr(args, "reason", None))
        print(f"state={change.get('state')}")
        return 0

    if action == "rollback":
        result = changes_mod.rollback_changeset(vault, args.change_id)
        print(_state_line(result))
        return 0

    print("usage: memex review [list|show|approve|reject|rollback] [change_id] [--reason REASON] [--vault VAULT]")
    return 1


def _state_line(result: dict) -> str:
    line = f"state={result.get('state')}"
    if result.get("reason"):
        line += f"  reason={result['reason']}"
    if result.get("error"):
        line += f"  error={result['error']}"
    return line
