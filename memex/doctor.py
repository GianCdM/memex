"""memex doctor — detect the environment.

Pure stdlib. Detects OS, RAM, arch and whether the Claude Code CLI is installed.
Surfaces vault status, hooks, skill, MCP, and extractors.
"""

from __future__ import annotations

import os
import platform
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
    print("Claude CLI:")
    print(f"  claude exe  : {'OK  ' + claude_path if has_claude else 'not found'}")
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
    print(f"  skill      : {'OK  Claude can search' if skill_mod.installed() else 'not installed   → memex skill install'}")
    mcp_path, has_mcp = hook_mod._mcp_status(Path.cwd())
    print(f"  MCP server : {'OK  memex tools available (.mcp.json)' if has_mcp else 'not wired   → run `memex init`'}")
    if has_mcp:
        print(f"               {mcp_path}")
        print("               approve it once in `/mcp` if status is pending")
    print()

    if has_claude:
        print("Completions: Claude Code CLI — log in with `claude /login` if not already.")
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
