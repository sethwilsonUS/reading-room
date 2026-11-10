"""Installation defaults shared by setup, diagnostics, and the library service.

This module must stay dependency-free: `scripts/setup.py` loads it with the
system interpreter before the checkout's virtual environment exists.
"""
from __future__ import annotations

from pathlib import Path

# Inclusive (major, minor) range. Keep in sync with `requires-python` in
# pyproject.toml; tests/test_onboarding.py enforces that.
MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 15)


def default_library_root() -> Path:
    """Books directory used when no --root, LIBRARY_ROOT, or saved binding exists."""
    return Path.home() / 'Documents' / 'Reading Room'


def supported_python(version: tuple[int, ...]) -> bool:
    """Return whether a `sys.version_info`-style tuple is a supported runtime."""
    return MIN_PYTHON <= tuple(version[:2]) <= MAX_PYTHON


def python_range() -> str:
    """Human-readable supported range, e.g. ``3.12–3.15``."""
    return f'{MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1]}'
