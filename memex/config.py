"""memex configuration — global (~/.config/memex) + per-vault (.memex/config.json).

Stdlib only. Completions always go through `claude -p --model <name>`;
embeddings are a separate HTTP endpoint (POST /embeddings).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Factory defaults — flat models + embeddings (no provider abstraction).
# The claude block is gone: completions always use `claude -p --model <name>`.
# Default model names are conservative (Claude Code resolves them).
DEFAULT_GLOBAL = {
    "models": {
        "propose": "haiku",
        "merge": "sonnet",
        "verify": "sonnet",
    },
    # embeddings are opt-in — base_url + model must be configured
    "embeddings": {
        "base_url": None,
        "api_key_env": None,
        "api_key_helper": None,
        "api_key": None,
        "model": None,
        "dimensions": None,
        "input_type": None,
        "query_input_type": None,
    },
    "workspaces": {},
}


def _migrate_cfg(cfg: dict) -> dict:
    """In-place migration from old provider shapes to flat models+embeddings.
    Never touches disk — only transforms the in-memory view so callers always
    see the new keys. Compat shim for one release; drop when all vaults are updated.

    Handles:
      - Global: provider.claude.model_propose → models.propose
      - Global: provider.embeddings → embeddings
      - Vault:  models.propose {model, dense} → models.propose string (drop dense)
      - Vault:  drop models.verify_chunk, models.propose_dense
    """
    # 1. Global: migrate provider.<name>.model_* to models.*
    if "models" not in cfg and "provider" in cfg:
        p = cfg["provider"]
        if isinstance(p, dict):
            models = {}
            order = p.get("order") or ["claude"]
            for prov_name in order:
                prov = p.get(prov_name)
                if isinstance(prov, dict):
                    for old_key, new_key in [("model_propose", "propose"),
                                             ("model_merge", "merge"),
                                             ("model_verify", "verify")]:
                        if old_key in prov and new_key not in models:
                            models[new_key] = prov[old_key]
                if models.get("propose") and models.get("merge"):
                    break
            if models:
                cfg["models"] = models

    # 2. Global: migrate provider.embeddings → top-level embeddings
    if "embeddings" not in cfg and "provider" in cfg:
        p = cfg["provider"]
        if isinstance(p, dict) and isinstance(p.get("embeddings"), dict):
            cfg["embeddings"] = dict(p["embeddings"])

    # 3. Vault: flatten models.propose from dict to string (drop dense/verify_chunk/etc)
    if "models" in cfg:
        m = cfg["models"]
        if isinstance(m.get("propose"), dict):
            m["propose"] = m["propose"].get("model") or "haiku"
        m.pop("verify_chunk", None)
        m.pop("propose_dense", None)

    return cfg


def global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "memex" / "config.json"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_user() -> dict:
    """The user's own config file, WITHOUT defaults merged — the only thing
    `config set` should ever mutate and persist. NOT migrated (save preserves
    old shape until the user edits)."""
    p = global_config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_global() -> dict:
    """Defaults + user overrides, migrated to the flat shape in-memory."""
    user = _migrate_cfg(load_user())  # migrate old shape BEFORE merging defaults
    return _merge(DEFAULT_GLOBAL, user)


def save_global(cfg: dict) -> None:
    p = global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def save_vault(vault: Path, cfg: dict) -> None:
    """Write the vault's .memex/config.json, preserving unknown keys."""
    p = Path(vault) / ".memex" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def load_vault(vault: Path) -> dict:
    """Load vault config, migrated to the flat shape in-memory."""
    p = Path(vault) / ".memex" / "config.json"
    if p.exists():
        try:
            return _migrate_cfg(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return {}
    return {}


def resolve_vault(explicit=None, workspace=None) -> Path:
    """Pick the vault without making the caller think about it:
    explicit --vault > this workspace's registered vault > global default > ~/memex.

    Used by every command so humans (and Claude, via the skill) can omit --vault
    anywhere; hooks still pass it explicitly."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    ws = str(Path(workspace or ".").expanduser().resolve())
    g = load_global()
    mapped = (g.get("workspaces") or {}).get(ws)
    if mapped:
        return Path(mapped).expanduser().resolve()
    if g.get("default_vault"):
        return Path(g["default_vault"]).expanduser().resolve()
    return Path("~/memex").expanduser().resolve()


def resolve_models(*, vault_cfg=None, global_cfg=None) -> dict:
    """Resolve model names after vault overlay.

    Returns {propose: str, merge: str, verify: str} — each is a model name
    string passed to `claude -p --model <name>`.

    Precedence: vault models.* > global models.* > DEFAULT_GLOBAL models.*
    """
    g = global_cfg or load_global()
    base = dict(DEFAULT_GLOBAL.get("models", {}))
    base.update(g.get("models", {}))
    if vault_cfg:
        v = vault_cfg.get("models") or {}
        for role in ("propose", "merge", "verify"):
            rv = v.get(role)
            if rv is not None:
                base[role] = rv
    return base


def resolve_verify_model(vault_cfg=None, *, global_cfg=None, default=None):
    """Resolve the verify model (fidelity judge).

    Precedence: verify > merge (same as resolve_models but fallback to merge,
    then to explicit default).
    """
    models = resolve_models(vault_cfg=vault_cfg, global_cfg=global_cfg)
    return models.get("verify") or models.get("merge") or default


def resolve_embeddings(*, vault_cfg=None, global_cfg=None):
    """Return (model, settings) for the optional embeddings endpoint, or
    (None, None) when not configured. A caller can treat None as
    "semantic recall is disabled" and fall back to lexical scoring.

    Precedence per key: vault embeddings.* > global embeddings.*
    A value of None in vault can NOT unset an inherited global value
    (null in JSON is ambiguous with absent). Users set api_key_helper
    on the vault block to override per-vault auth.
    """
    g = global_cfg or load_global()
    global_embed = g.get("embeddings") or {}
    vault_embed = (vault_cfg or {}).get("embeddings") or {}
    settings = {**global_embed, **{k: v for k, v in vault_embed.items() if v is not None}}
    model = settings.get("model")
    base = settings.get("base_url")
    if not model or not base:
        return None, None
    return model, settings