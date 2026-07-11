"""Portable process helpers — pid liveness, detached spawn, executable discovery.

Windows-first, because hooks must work everywhere the harness does:
- `os.kill(pid, 0)` is NOT a liveness probe on Windows — any signal other than
  CTRL_C/CTRL_BREAK unconditionally TerminateProcess-es the target. pid_alive
  uses OpenProcess/GetExitCodeProcess there instead.
- Detached spawn uses DETACHED_PROCESS on Windows and start_new_session on
  POSIX — no `nohup ... &` shell tricks, which break across cmd/PowerShell/
  Git Bash and made the v1 hooks POSIX-only.
- Hook commands can't rely on PATH (the harness may run with its own env), so
  memex_exe()/claude_exe() resolve absolute paths for embedding in hooks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_WIN = sys.platform == "win32"


def pid_alive(pid) -> bool:
    """True if a process with this pid currently exists. Never signals it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _WIN:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # handle opened but query failed — assume alive
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 = existence probe
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except Exception:
        return False
    return True


def _first_existing(candidates):
    for c in candidates:
        try:
            if c and Path(c).is_file():
                return str(Path(c))
        except OSError:
            continue
    return None


def memex_exe() -> str:
    """Absolute path to the memex launcher — embedded in hook commands so they
    work even when the harness spawns hooks with a PATH that lacks ~/.local/bin."""
    exe = shutil.which("memex")
    if exe:
        return str(Path(exe))
    names = ("memex.exe", "memex") if _WIN else ("memex",)
    # the ~/.local/bin shim is the canonical uv/pipx location and survives venv
    # rebuilds — prefer it over argv[0] (which may point into a tool venv)
    found = _first_existing(
        [Path.home() / ".local" / "bin" / n for n in names]
        + [Path(sys.argv[0])]
        + [Path(sys.executable).with_name(n) for n in names]
    )
    return found or "memex"


def claude_exe():
    """Absolute path to the claude CLI, or None. PATH first, then the default
    native-install location (~/.local/bin), which is often NOT on PATH."""
    exe = shutil.which("claude")
    if exe:
        return str(Path(exe))
    names = ("claude.exe", "claude") if _WIN else ("claude",)
    return _first_existing([Path.home() / ".local" / "bin" / n for n in names])


def spawn_detached(argv, cwd=None) -> int:
    """Fire-and-forget a child that outlives this process (and the hook that
    called it). Returns the pid, or 0 on failure — never raises."""
    try:
        kwargs = dict(
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if _WIN:
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(argv, **kwargs).pid
    except Exception:
        return 0
