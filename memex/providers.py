"""LLM completion + embeddings for the synth step.

Completions: always through `claude -p --model <name>` — the Claude Code CLI
  resolves the model (Anthropic, OpenRouter, GenPlat, etc.). Memex never talks
  to a completion endpoint directly.

Embeddings: HTTP POST to an OpenAI-compatible /embeddings endpoint. Anthropic's
  Messages API doesn't do embeddings, so this stays separate.

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
    """Reasoning models (e.g. deepseek-r1) wrap chain-of-thought in  ...  tags."""
    return re.sub(r" .*? ", "", text, flags=re.DOTALL).strip()


def complete(prompt: str, *, model: str, settings: dict | None = None,
             allowed_tools=None) -> str:
    """Run one completion via `claude -p --model <name>`.

    `prompt` goes to stdin (avoids CLI length limits). `allowed_tools` is a
    narrow MCP allowlist for cloud-doc resolution (e.g.
    ["mcp__google-workspace__get_doc_as_markdown"]) — no blanket permission.
    Returns the model's stdout. Raises ProviderError on any failure.
    """
    if settings is None:
        settings = {}
    from . import proc
    exe = proc.claude_exe()
    if not exe:
        raise ProviderError("`claude` CLI not found (PATH or ~/.local/bin). Install Claude Code.")
    cmd = [exe, "-p"]
    if model:
        cmd += ["--model", model]
    env, stdin_text = None, prompt
    if allowed_tools:
        cmd += ["--allowedTools", " ".join(allowed_tools), "--output-format", "json"]
        env = {**os.environ, "MCP_TIMEOUT": str(settings.get("mcp_timeout", 20000))}
    try:
        out = subprocess.run(
            cmd, **proc.run_kwargs(
                capture_output=True, text=True, timeout=settings.get("timeout", 600),
                cwd=tempfile.gettempdir(),
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
    stdout = (out.stdout or "").strip()
    if not stdout:
        err_tail = (out.stderr or "").strip()[-300:]
        raise ProviderError(f"`claude -p` returned no output (rc=0). stderr: {err_tail or '(empty)'}")
    if allowed_tools:
        try:
            return (json.loads(stdout).get("result") or "").strip()
        except Exception:
            return stdout
    return stdout


def _resolve_api_key(settings: dict) -> str | None:
    """Get the current API key without persisting it.

    Precedence:
      1. env var named by settings["api_key_env"]
      2. stdout of settings["api_key_helper"] shell command
      3. settings["api_key"] literal
    Returns None if none yield a value. Raises ProviderError on helper failure.
    """
    env_var = settings.get("api_key_env")
    if env_var and os.environ.get(env_var):
        return os.environ[env_var].strip()
    helper = settings.get("api_key_helper")
    if helper:
        try:
            out = subprocess.run(
                helper, shell=True, capture_output=True, text=True,
                timeout=settings.get("api_key_helper_timeout", 30),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise ProviderError(f"api_key_helper failed: {e}")
        if out.returncode != 0:
            detail = (out.stderr or out.stdout or "").strip()[:200]
            raise ProviderError(f"api_key_helper exit {out.returncode}: {detail}")
        key = (out.stdout or "").strip()
        if key:
            return key
    literal = settings.get("api_key")
    return literal if literal else None


def embed(inputs, *, model: str, settings: dict) -> list[list[float]]:
    """Turn one or more strings into embedding vectors via an OpenAI-compatible
    endpoint (POST /embeddings). Anthropic's Messages API does not do embeddings,
    so this is a separate HTTP call.

    Always returns a list of vectors (one per input). Raises ProviderError on
    transport / auth / schema failures so the caller can degrade gracefully
    (fall back to lexical recall). Does NOT auto-batch — the caller decides.
    """
    if isinstance(inputs, str):
        inputs = [inputs]
    if not inputs:
        return []
    base = (settings.get("base_url") or "").rstrip("/")
    if not base:
        raise ProviderError("embeddings: no base_url configured")
    url = base + "/embeddings"
    payload = {"model": model, "input": inputs}
    # Only send input_type/query_input_type when explicitly configured.
    # Many providers (OpenAI, Voyage) ignore the field; Nvidia/Cohere require it.
    if settings.get("input_type"):
        payload["input_type"] = settings["input_type"]
    headers = {"Content-Type": "application/json"}
    key = _resolve_api_key(settings)
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.get("timeout", 60)) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:500]
        except Exception:
            pass
        raise ProviderError(f"embeddings HTTP {e.code} at {url}: {detail or e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"embeddings endpoint error at {url}: {e}")
    try:
        items = data["data"]
        items = sorted(items, key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]
    except (KeyError, TypeError):
        raise ProviderError(f"embeddings: unexpected response shape: {json.dumps(data)[:500]}")