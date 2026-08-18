"""memex — a portable, local-first second brain built from your AI sessions."""

def _read_version():
    # Single source of truth: pyproject.toml [project] version. Installed
    # package exposes it via metadata; when running from source (tests, dev),
    # read the pyproject.toml one dir up. Never a hand-edited literal here.
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("memex")
    except Exception:
        pass
    try:
        from pathlib import Path
        import re
        toml = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', toml, re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0"


__version__ = _read_version()