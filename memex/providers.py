"""LLM provider backends for the synth step.

Two backends cover everything, so memex stays vendor- and platform-agnostic:
  - "claude"        -> the `claude` CLI (`claude -p --model ...`)
  - "openai_compat" -> any OpenAI-compatible HTTP endpoint
                       (Ollama, LM Studio, vLLM, llama.cpp, OpenAI, Together, ...)

Stdlib only (urllib for HTTP). The LLM only runs here (synth), never in hooks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


def _strip_think(text: str) -> str:
    """Reasoning models (e.g. deepseek-r1) wrap chain-of-thought in <think>...</think>."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _complete_claude(prompt: str, model: str, settings: dict, allowed_tools=None) -> str:
    from . import proc
    exe = proc.claude_exe()  # PATH, else ~/.local/bin (often NOT on PATH on Windows)
    if not exe:
        raise ProviderError("`claude` CLI not found (PATH or ~/.local/bin). Install Claude Code.")
    cmd = [exe, "-p"]
    if model:
        cmd += ["--model", model]
    env, stdin_text = None, None
    if allowed_tools:
        # allow ONLY these MCP tools — a narrow allowlist, never a blanket bypass.
        # prompt via STDIN (the variadic --allowedTools would swallow a positional
        # prompt), and give the HTTP MCP gateway time to connect (loads async).
        cmd += ["--allowedTools", " ".join(allowed_tools), "--output-format", "json"]
        stdin_text = prompt
        env = {**os.environ, "MCP_TIMEOUT": str(settings.get("mcp_timeout", 20000))}
    else:
        cmd += [prompt]
    from . import proc
    try:
        out = subprocess.run(
            cmd, **proc.run_kwargs(
                capture_output=True, text=True, timeout=settings.get("timeout", 600),
                cwd=tempfile.gettempdir(),  # isolate from any workspace's memex hooks
                input=stdin_text, env=env,
            )
        )
    except FileNotFoundError:
        raise ProviderError("`claude` CLI not found on PATH (install Claude Code).")
    except subprocess.TimeoutExpired:
        raise ProviderError("`claude -p` timed out.")
    combined = f"{out.stdout or ''}\n{out.stderr or ''}"
    if "not logged in" in combined.lower():
        raise ProviderError(
            "`claude` CLI is not logged in — run `claude /login` once (interactive), "
            "then re-run; pending notes synthesize automatically on the next session end.")
    if out.returncode != 0:
        detail = (out.stderr or "").strip() or (out.stdout or "").strip()
        raise ProviderError(f"`claude -p` failed (rc={out.returncode}): {detail[:500]}")
    if allowed_tools:  # --output-format json -> the answer (after tool use) is in .result
        try:
            return (json.loads(out.stdout).get("result") or "").strip()
        except Exception:
            return out.stdout.strip()
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


def complete(prompt: str, *, kind: str, model: str, settings: dict,
             json_mode: bool = False, allowed_tools=None) -> str:
    """Run one completion. `kind` is 'claude' or 'openai_compat'.

    json_mode=True asks an OpenAI-compatible endpoint (Ollama/LM Studio/...) to
    constrain the output to valid JSON. claude relies on the prompt (reliable enough).
    allowed_tools (claude only) is a narrow MCP allowlist (e.g.
    ["mcp__google-workspace__get_doc_as_markdown"]) — lets the headless model resolve
    a cloud doc via that one tool, with no blanket permission bypass.
    """
    if kind == "claude":
        return _complete_claude(prompt, model, settings, allowed_tools=allowed_tools)
    return _complete_openai_compat(prompt, model, settings, json_mode=json_mode)
