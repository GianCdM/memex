"""memex doctor — detect the environment and recommend a provider/model setup.

Pure stdlib. Detects OS, RAM, arch and which LLM providers are reachable
(claude CLI / ollama), then recommends models sized to the machine.
Read-only for now (writing config comes in a later phase).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess


def total_ram_gb():
    """Best-effort total physical RAM in GB, cross-platform. None if unknown."""
    system = platform.system()
    try:
        if system == "Darwin":
            from . import proc
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                **proc.run_kwargs(capture_output=True, text=True, timeout=5),
            )
            return int(out.stdout.strip()) / (1024 ** 3)
        if system == "Linux":
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 ** 2)
        if system == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        return None
    return None


def recommend_local_models(ram_gb: float):
    """Map a usable-memory budget to (propose, merge, note) Ollama models.

    Total/unified RAM is not all usable for the model, so we budget ~65%.
    """
    budget = ram_gb * 0.65
    if budget < 6:
        return ("llama3.2:3b", "qwen2.5:7b",
                "tight RAM - small models only; consider a cloud provider.")
    if budget < 10:
        return ("qwen2.5:7b", "phi4:14b", None)
    if budget < 20:
        return ("qwen2.5:7b", "deepseek-r1:14b",
                "optional push for quality: qwen3:32b (slower / tighter).")
    if budget < 34:
        return ("qwen2.5:7b", "qwen3:32b", None)
    return ("qwen2.5:7b", "llama3.3:70b", None)


def _install_hint(system: str) -> str:
    if system == "Darwin":
        return "https://ollama.com/download  (or: brew install ollama)"
    if system == "Linux":
        return "curl -fsSL https://ollama.com/install.sh | sh"
    if system == "Windows":
        return "https://ollama.com/download  (.exe installer)"
    return "https://ollama.com/download"


# Consensus for optional extractors (used by `ingest --docs` for documents & media):
#   - uv-first for anything that's a Python tool (works on every OS, no sudo);
#   - the OS package manager ONLY for native binaries (ffmpeg, tesseract) — no way around those.
# markitdown[all] is the 80/20: ONE uv command covers every document type, pure-Python.
def _extractor_install_cmd(tool: str, system: str) -> str:
    py_uv = {
        "markitdown": "uv tool install 'markitdown[all]'   (or: pipx install 'markitdown[all]')",
        "whisper":    "uv tool install openai-whisper       (or: pipx install openai-whisper)",
    }
    native = {
        "ffmpeg":    {"Darwin": "brew install ffmpeg",
                      "Linux":  "sudo apt install ffmpeg   (or: sudo dnf install ffmpeg)",
                      "Windows": "winget install Gyan.FFmpeg   (or: choco install ffmpeg)"},
        "tesseract": {"Darwin": "brew install tesseract",
                      "Linux":  "sudo apt install tesseract-ocr",
                      "Windows": "winget install UB-Mannheim.TesseractOCR"},
    }
    if tool in py_uv:
        return py_uv[tool]
    return native.get(tool, {}).get(system, f"(install {tool} for your platform)")


def run(args) -> int:
    from pathlib import Path

    from . import config as config_mod
    from . import hook as hook_mod
    from . import proc
    from . import skill as skill_mod

    system = platform.system()
    machine = platform.machine()
    ram = total_ram_gb()
    apple_silicon = system == "Darwin" and machine in ("arm64", "aarch64")
    claude_path = proc.claude_exe()
    has_claude = claude_path is not None
    has_ollama = shutil.which("ollama") is not None

    print("memex doctor")
    print("=" * 44)
    print(f"OS          : {system} ({machine})")
    if ram:
        suffix = " unified (Apple Silicon)" if apple_silicon else ""
        print(f"RAM         : {ram:.0f} GB{suffix}")
    else:
        print("RAM         : unknown")
    print(f"CPU cores   : {os.cpu_count()}")
    print()
    print("Detected providers:")
    print(f"  claude CLI : {'OK  ' + claude_path if has_claude else 'not found'}")
    print(f"  ollama     : {'OK' if has_ollama else 'not installed'}")
    print()

    # ── the brain loop on THIS machine/workspace ──
    vault_dir = config_mod.resolve_vault(None)
    vault_ok = (vault_dir / ".memex").exists()
    print("Brain loop:")
    print(f"  memex exe  : {proc.memex_exe()}")
    print(f"  vault      : {'OK  ' if vault_ok else 'MISSING  '}{vault_dir}"
          + ("" if vault_ok else "   → run `memex init`"))
    _, hooks_found = hook_mod._status(Path.cwd())
    events = {e for e, _ in hooks_found}
    wanted = {"SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact"}
    if events >= wanted:
        print("  hooks      : OK  boot + recall + capture + pre-compact (this workspace)")
    elif events:
        missing = ", ".join(sorted(wanted - events))
        print(f"  hooks      : PARTIAL (missing: {missing})   → re-run `memex init`")
    else:
        print("  hooks      : none in this workspace   → run `memex init`")
    print(f"  skill      : {'OK  Claude can search/remember/handoff' if skill_mod.installed() else 'not installed   → memex skill install'}")
    print()

    if has_claude:
        print("Recommendation: the `claude` provider (cloud, best quality) — it is")
        print("  already first in the default order; nothing to configure.")
        print()

    if ram:
        propose, merge, note = recommend_local_models(ram)
        print("Suggested local (Ollama) setup for this machine:")
        print(f"  propose : {propose}")
        print(f"  merge   : {merge}")
        if note:
            print(f"  note    : {note}")
        if not has_ollama:
            print(f"  install ollama: {_install_hint(system)}")
        print()

    # ── optional extractors for `ingest --docs` (documents & media) ──
    try:
        from . import extract as extract_mod
        have = extract_mod.available()
    except Exception:
        have = {}
    print("Extractors for `ingest --docs` (documents & media — all OPTIONAL):")
    print("  ► one command covers every DOCUMENT (pdf·docx·pptx·xlsx), pure-Python, any OS:")
    print("      uv tool install 'markitdown[all]'")
    print()
    for tool, what in [
        ("markitdown", "documents → Markdown (pdf · docx · pptx · xlsx)"),
        ("whisper",    "audio & video transcription — LOCAL (needs ffmpeg)"),
        ("ffmpeg",     "audio/video decoding"),
        ("tesseract",  "OCR for scanned images"),
    ]:
        if have.get(tool):
            print(f"  ✓ {tool:11s} {what}")
        else:
            print(f"  – {tool:11s} {what}")
            print(f"      → {_extractor_install_cmd(tool, system)}")
    print()
    print("  memex runs with whatever you have — a missing tool just skips that file")
    print("  type (never crashes). uv-first for Python tools; OS package manager for native.")
    print()
    print("(doctor is read-only for now; writing config comes in a later phase.)")
    return 0
