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
        # Optional: OpenAI-compatible embeddings provider for semantic recall.
        # Anthropic Messages API has no embeddings endpoint, so this is a
        # SEPARATE HTTP provider — memex still uses `claude -p` for generation,
        # and only touches this when you enable it. Leave `base_url` empty to
        # disable (recall falls back to the lexical Jaccard/IDF scorer).
        # Example configs:
        #   - OpenAI:         base_url=https://api.openai.com/v1, model=text-embedding-3-small
        #   - Voyage:         base_url=https://api.voyageai.com/v1, model=voyage-3-lite
        #   - Cohere:         base_url=https://api.cohere.com/v2, model=embed-multilingual-v3.0
        #   - Ollama local:   base_url=http://localhost:11434/v1, model=nomic-embed-text
        #   - Any OpenAI-compatible gateway (self-hosted proxies, corporate LLM gateways)
        # `input_type` (optional) is required by Cohere/Bedrock and ignored by
        # the others — set to "search_document" for indexing and the recall step
        # switches it to "search_query" automatically.
        "embeddings": {
            "base_url": None,
            # Auth: pick ONE of the three (checked in this order):
            #   api_key_env    — env var name to read at request time
            #                    (e.g. "OPENAI_API_KEY")
            #   api_key_helper — shell command whose stdout is the token
            #                    (same pattern as Claude Code's apiKeyHelper) —
            #                    never persist a short-lived token on disk
            #   api_key        — literal fallback (avoid: file readable by any process)
            "api_key_env": None,
            "api_key_helper": None,
            "api_key": None,
            "model": None,
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
    return name, kind, settings


def resolve_verify_model(vault_cfg=None, *, default=None):
    """Resolve the strong-judge (fidelity verify) model.

    Precedence: per-vault `models.verify` (nested, consistent with the other
    model knobs) > legacy top-level `verify_model` > `default`.
    """
    if vault_cfg:
        m = vault_cfg.get("models") or {}
        if m.get("verify"):
            return m["verify"]
        if vault_cfg.get("verify_model"):
            return vault_cfg["verify_model"]
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
