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
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
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


def run(args) -> int:
    system = platform.system()
    machine = platform.machine()
    ram = total_ram_gb()
    apple_silicon = system == "Darwin" and machine in ("arm64", "aarch64")
    has_claude = shutil.which("claude") is not None
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
    print(f"  claude CLI : {'OK' if has_claude else 'not found'}")
    print(f"  ollama     : {'OK' if has_ollama else 'not installed'}")
    print()

    if has_claude:
        print("Recommendation: use the `claude` provider (cloud, best quality).")
        print("  -> memex config set provider claude   [coming soon]")
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

    print("(doctor is read-only for now; writing config comes in a later phase.)")
    return 0
