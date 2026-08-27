"""LLM completion + embeddings for the synth step.

Completions: always through `claude -p --model <name>` — the Claude Code CLI
  resolves the model (Anthropic, OpenRouter, a corporate gateway, etc.). Memex
  never talks to a completion endpoint directly.

Embeddings: HTTP POST to an OpenAI-compatible /embeddings endpoint. Anthropic's
  Messages API doesn't do embeddings, so this stays separate.

Stdlib only (urllib for HTTP). The LLM only runs here (synth), never in hooks.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


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


def _retry_after_seconds(err: urllib.error.HTTPError, body: str) -> float | None:
    """Best-effort wait hint for a 429. Prefers the Retry-After header, then the
    GenPlat `Limit resets at: <UTC>` body line, else None (caller backoff)."""
    ra = (err.headers or {}).get("Retry-After")
    if ra:
        try:
            return float(ra)
        except (ValueError, TypeError):
            pass
    m = re.search(r"Limit resets at:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*UTC", body)
    if m:
        try:
            reset = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            return (reset - datetime.now(timezone.utc)).total_seconds()
        except ValueError:
            return None
    return None


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
    req_data = json.dumps(payload).encode()
    max_attempts = int(settings.get("embed_max_attempts", 6))
    base_delay = float(settings.get("embed_retry_base", 2.0))
    max_wait = float(settings.get("embed_retry_max_wait", 180.0))
    data = None
    last_err: ProviderError | None = None
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(
            url, data=req_data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=settings.get("timeout", 60)) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            last_err = ProviderError(
                f"embeddings HTTP {e.code} at {url}: {body[:500] or e.reason}"
            )
            # 429 = rate limit: wait out the reset window (Retry-After or the
            # GenPlat `Limit resets at:` body). 5xx = transient upstream — back
            # off. Otherwise a full vault sweep dies on the first busy minute
            # or dropped connection and never completes.
            retryable = e.code == 429 or 500 <= e.code <= 599
            if retryable and attempt < max_attempts:
                if e.code == 429:
                    wait = _retry_after_seconds(e, body)
                    if wait is None:
                        wait = min(base_delay * (2 ** (attempt - 1)), max_wait)
                        wait += random.uniform(0.0, 0.25 * wait)
                else:
                    wait = min(base_delay * (2 ** (attempt - 1)), max_wait)
                    wait += random.uniform(0.0, 0.25 * wait)
                wait = min(max(wait, 0.0), max_wait)
                print(f"    embeddings {e.code} — retrying in {wait:.0f}s "
                      f"(attempt {attempt}/{max_attempts})", flush=True)
                time.sleep(wait)
                continue
            raise last_err
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = ProviderError(f"embeddings endpoint error at {url}: {e}")
            if attempt < max_attempts:
                wait = min(base_delay * (2 ** (attempt - 1)), max_wait)
                time.sleep(wait)
                continue
            raise last_err
    if data is None:
        raise last_err or ProviderError("embeddings: no response")
    try:
        items = data["data"]
        items = sorted(items, key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]
    except (KeyError, TypeError):
        raise ProviderError(f"embeddings: unexpected response shape: {json.dumps(data)[:500]}")