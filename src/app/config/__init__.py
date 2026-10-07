"""Configuration package.

Re-exports the public surface of the configuration submodules so that
callers can write ``from app.config import settings`` instead of
reaching into :mod:`app.config.settings`. This keeps the import paths
in the rest of the codebase stable even if the internal file layout
changes.

Importing this package has side effects inherited from
:mod:`app.config.settings`: ``.env`` is loaded and the ``settings``
singleton is built. That is intentional — the package is the sanctioned
place for environment access — but it means tests that want a clean
environment should import :mod:`app.config.settings` directly and
instantiate :class:`Settings` themselves.
"""

from __future__ import annotations

from app.config.logging import setup_logging, shutdown_logging
from app.config.selectors import Selectors, load_selectors
from app.config.settings import (
    ConfigError,
    Settings,
    settings,
)

__all__ = [
    "ConfigError",
    "Selectors",
    "Settings",
    "load_selectors",
    "settings",
    "setup_logging",
    "shutdown_logging",
]
