"""memex configuration — global (~/.config/memex) + per-vault (.memex/config.json).

Stdlib only. Decides which provider/model the synth step uses. Provider choice
is GLOBAL (not per-tier); tiers only govern edit behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Provider name -> backend kind understood by providers.complete().
PROVIDER_KIND = {
    "claude": "claude",
    "ollama": "openai_compat",
    "openai": "openai_compat",
    "lmstudio": "openai_compat",
    "vllm": "openai_compat",
    "openai_compat": "openai_compat",
}

DEFAULT_GLOBAL = {
    "provider": {
        "order": ["claude", "ollama"],
        "claude": {"model_propose": "haiku", "model_merge": "sonnet"},
        "ollama": {
            "base_url": "http://localhost:11434/v1",
            "api_key": None,
            "model_propose": "qwen2.5:7b",
            "model_merge": "deepseek-r1:14b",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": None,
            "model_propose": "gpt-4o-mini",
            "model_merge": "gpt-4o",
        },
    },
    "workspaces": {},
}


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


def load_global() -> dict:
    p = global_config_path()
    user = {}
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            user = {}
    return _merge(DEFAULT_GLOBAL, user)


def save_global(cfg: dict) -> None:
    p = global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def load_vault(vault: Path) -> dict:
    p = Path(vault) / ".memex" / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
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


def resolve_provider(provider_name=None, *, vault_cfg=None, global_cfg=None):
    """Resolve a provider for a synth run.

    Returns (name, kind, settings) where settings has base_url/api_key/
    model_propose/model_merge. Precedence for models: per-vault override >
    global provider config. `provider_name` overrides the global order.
    """
    g = global_cfg or load_global()
    pconf = g.get("provider", {})
    order = pconf.get("order") or ["claude"]
    name = provider_name or order[0]
    kind = PROVIDER_KIND.get(name, "openai_compat")
    settings = dict(pconf.get(name, {}))
    if vault_cfg:
        models = vault_cfg.get("models") or {}
        if models.get("propose"):
            settings["model_propose"] = models["propose"]
        if models.get("merge"):
            settings["model_merge"] = models["merge"]
    return name, kind, settings
