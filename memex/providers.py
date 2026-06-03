"""LLM provider backends for the synth step.

Two backends cover everything, so memex stays vendor- and platform-agnostic:
  - "claude"        -> the `claude` CLI (`claude -p --model ...`)
  - "openai_compat" -> any OpenAI-compatible HTTP endpoint
                       (Ollama, LM Studio, vLLM, llama.cpp, OpenAI, Together, ...)

Stdlib only (urllib for HTTP). The LLM only runs here (synth), never in hooks.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


def _strip_think(text: str) -> str:
    """Reasoning models (e.g. deepseek-r1) wrap chain-of-thought in <think>...</think>."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _complete_claude(prompt: str, model: str, settings: dict) -> str:
    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=settings.get("timeout", 600)
        )
    except FileNotFoundError:
        raise ProviderError("`claude` CLI not found on PATH (install Claude Code).")
    except subprocess.TimeoutExpired:
        raise ProviderError("`claude -p` timed out.")
    if out.returncode != 0:
        raise ProviderError(f"`claude -p` failed: {out.stderr.strip()[:500]}")
    return out.stdout.strip()


def _complete_openai_compat(prompt: str, model: str, settings: dict, json_mode: bool = False) -> str:
    base = (settings.get("base_url") or "http://localhost:11434/v1").rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": settings.get("temperature", 0.2),
    }
    if json_mode:
        # grammar-constrained JSON (Ollama / OpenAI-compatible) -> always parseable
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = "Bearer " + settings["api_key"]
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.get("timeout", 600)) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(
            f"OpenAI-compatible endpoint error at {url}: {e}. "
            "Server unreachable, timed out, or busy (e.g. pulling a model while serving)."
        )
    try:
        return _strip_think(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected response shape: {json.dumps(data)[:500]}")


def complete(prompt: str, *, kind: str, model: str, settings: dict, json_mode: bool = False) -> str:
    """Run one completion. `kind` is 'claude' or 'openai_compat'.

    json_mode=True asks an OpenAI-compatible endpoint (Ollama/LM Studio/...) to
    constrain the output to valid JSON. claude relies on the prompt (reliable enough).
    """
    if kind == "claude":
        return _complete_claude(prompt, model, settings)
    return _complete_openai_compat(prompt, model, settings, json_mode=json_mode)
