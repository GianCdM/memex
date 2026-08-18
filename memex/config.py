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
    "openrouter": "openai_compat",
    "openai": "openai_compat",
    "lmstudio": "openai_compat",
    "vllm": "openai_compat",
    "openai_compat": "openai_compat",
}

# Each provider declares all three model roles explicitly so the vault can
# override any of them per-project. The legacy implicit fallback (verify →
# merge) is preserved in resolve_verify_model for backward compat, but
# providers now carry their own opinion on every role instead of relying on
# that fallback being the only source of truth.
#
# model roles (3 active + 2 legacy aliases for backward compat):
#   model_propose          — distill a raw session/delta into a candidate wiki page
#   model_propose_dense    — DEPRECATED: alias for model_propose (still read from vault
#                            config for backward compat, ignored)
#   model_merge            — merge a candidate with existing wiki state
#   model_verify           — judge body fidelity of a candidate (the "auto_review" judge)
#   model_verify_chunk     — DEPRECATED: alias for model_verify (still read from vault
#                            config for backward compat, ignored)
DEFAULT_GLOBAL = {
    "provider": {
        "order": ["claude"],
        # claude: Claude Code CLI — uses the Anthropic account the user logged in with.
        "claude": {
            "model_propose": "haiku",
            "model_merge": "sonnet",
            "model_verify": "sonnet",
        },
        # openrouter: free, multilingual (34 languages) models via OpenRouter gateway.
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "api_key_helper": None,
            "api_key": None,
            "model_propose": "nvidia/nemotron-3-nano-omni",
            "model_merge": "nvidia/nemotron-3-ultra",
            "model_verify": "nvidia/nemotron-3-ultra",
        },
        # embeddings: a separate capability (POST /embeddings), not a completion
        # provider. Lives here for grouping, but is resolved independently.
        "embeddings": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "api_key_helper": None,
            "api_key": None,
            "model": "nvidia/nemotron-3-embed-1b",
            "dimensions": None,
            "input_type": None,
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


def load_user() -> dict:
    """The user's own config file, WITHOUT defaults merged — the only thing
    `config set` should ever mutate and persist."""
    p = global_config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_global() -> dict:
    return _merge(DEFAULT_GLOBAL, load_user())


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
    # `embeddings` lives under provider.* for grouping, but it's a separate
    # capability (no LLM completion) — never treat it as a generation provider.
    if name == "embeddings":
        raise ValueError("`embeddings` is not a completion provider — configure it separately via provider.embeddings.*")
    kind = PROVIDER_KIND.get(name, "openai_compat")
    settings = dict(pconf.get(name, {}))
    if vault_cfg:
        models = vault_cfg.get("models") or {}
        if models.get("propose"):
            p = models["propose"]
            # `propose` is a string (legacy) or `{model, dense}` (nested tiers)
            if isinstance(p, dict):
                if p.get("model"):
                    settings["model_propose"] = p["model"]
                if p.get("dense"):
                    settings["model_propose_dense"] = p["dense"]
            else:
                settings["model_propose"] = p
        if models.get("propose_dense"):
            # legacy flat knob — kept for backward compat
            settings["model_propose_dense"] = models["propose_dense"]
        if models.get("merge"):
            settings["model_merge"] = models["merge"]
        if models.get("verify"):
            settings["model_verify"] = models["verify"]
        if models.get("verify_chunk"):
            settings["model_verify_chunk"] = models["verify_chunk"]
    return name, kind, settings


def resolve_verify_model(vault_cfg=None, *, default=None, global_cfg=None):
    """Resolve the strong-judge (fidelity verify) model.

    Precedence:
      1. per-vault `models.verify` (explicit override)
      2. per-vault legacy `verify_model` (kept for backward compat)
      3. global provider's `model_verify` (explicit role in DEFAULT_GLOBAL)
      4. fallback `default` (legacy: usually model_merge)
    """
    if vault_cfg:
        m = vault_cfg.get("models") or {}
        if m.get("verify"):
            return m["verify"]
        if vault_cfg.get("verify_model"):
            return vault_cfg["verify_model"]
    # global provider config may carry an explicit model_verify (DEFAULT_GLOBAL).
    # When the caller didn't supply one, load it ourselves — the metric-recording
    # sites below pass only vcfg, not the full global_cfg.
    g = (global_cfg or load_global()).get("provider") or {}
    for pname in (g.get("order") or ["claude"]):
        pconf = g.get(pname, {})
        mv = pconf.get("model_verify")
        if mv:
            return mv
    return default


def resolve_embeddings(*, vault_cfg=None, global_cfg=None):
    """Return (model, settings) for the optional embeddings provider, or
    (None, None) when it's not configured. A caller can treat None as
    "semantic recall is disabled" and fall back to lexical scoring.

    Precedence for embeddings config (each key resolved independently):
      per-vault embeddings.* > global provider.embeddings.*
    """
    g = global_cfg or load_global()
    global_embed = (g.get("provider") or {}).get("embeddings") or {}
    vault_embed = (vault_cfg or {}).get("embeddings") or {}
    settings = {**global_embed, **{k: v for k, v in vault_embed.items() if v is not None}}
    model = settings.get("model")
    base = settings.get("base_url")
    if not model or not base:
        return None, None
    return model, settings
