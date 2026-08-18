"""memex — a portable, local-first second brain built from your AI sessions."""

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("memex")
except Exception:  # not installed (running from source) — dev fallback
    __version__ = "0.0.0"