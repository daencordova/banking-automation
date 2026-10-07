"""Playwright browser lifecycle, wrapped in a context manager.

Everything related to launching, reusing and tearing down the Chromium
instance lives here. Callers only ever see a :class:`BrowserSession`
that they enter with ``with`` and from which they request pages.

Two design choices deserve a note:

1. **Persistent context, not per-cycle launch.** The bot reuses the
   same ``BrowserContext`` across the entire process. Cookies therefore
   survive between cycles in memory, and are only serialized to
   ``state.json`` on clean shutdown. This keeps the "warm" session path
   (which is the common case) free of disk I/O.

2. **Best-effort shutdown.** Closing a Playwright resource is not
   guaranteed to succeed: if a previous step already tore down the
   driver, ``.close()`` and ``.stop()`` raise
   ``"Connection closed while reading from the driver"``. We treat that
   specific message as benign and log everything else at debug level.
   Losing a shutdown to a traceback would mask the real error that
   caused the shutdown in the first place.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from app import config

if TYPE_CHECKING:
    # Only needed for the ``__exit__`` signature.
    from types import TracebackType

log = logging.getLogger(__name__)


class BrowserSession:
    """Context manager owning the Playwright driver, browser and context.

    Usage::

        with BrowserSession(settings) as session:
            page = session.new_page()
            ...

    On exit, if :meth:`mark_dirty` was called during the session, the
    current storage state is written to ``settings.storage_state``. Only
    successful logins mark the session dirty, so a failed login never
    overwrites a previously good state file.
    """

    def __init__(self, settings: config.Settings = config.settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        # Tracks whether the in-memory storage state has changed since
        # the last time it was loaded. Only written to disk when True,
        # to avoid clobbering a valid state file after a failed login.
        self._dirty: bool = False
        self.started_at: float | None = None

    def __enter__(self) -> BrowserSession:
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=self.settings.headless,
            slow_mo=self.settings.slow_mo,
        )

        # Locale and timezone are pinned to the bank's home region so
        # that date/time widgets and locale-sensitive selectors behave
        # as they do for a real user in Venezuela.
        context_kwargs = {
            "viewport": {"width": 1366, "height": 900},
            "locale": "es-VE",
            "timezone_id": "America/Caracas",
        }

        state_path = self.settings.storage_state
        if state_path.exists():
            try:
                self.context = self.browser.new_context(
                    storage_state=str(state_path), **context_kwargs
                )
                log.info("Storage state loaded from %s", state_path)
            except Exception:
                # A corrupt or outdated state file should not prevent the
                # bot from starting; it just means a fresh login will be
                # performed this run.
                log.warning(
                    "Could not load storage state from %s; starting fresh.",
                    state_path,
                    exc_info=True,
                )
                self.context = self.browser.new_context(**context_kwargs)
        else:
            self.context = self.browser.new_context(**context_kwargs)
            log.info("No saved session found, starting fresh.")

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # Persist only when the session actually changed something.
        # ``__exit__`` runs even on exception, so the dirty flag is what
        # distinguishes "we logged in successfully" from "we crashed
        # halfway through a login attempt".
        if self._dirty and self.context is not None:
            try:
                self.context.storage_state(path=str(self.settings.storage_state))
                if os.name == "posix":
                    # state.json holds live cookies; restrict access so
                    # it is not world-readable on multi-user systems.
                    os.chmod(self.settings.storage_state, 0o600)
                log.info("Session saved to %s", self.settings.storage_state)
            except Exception:
                # Persisting is best-effort: losing the state only costs
                # us a re-login next run, so we log and continue.
                log.exception("Failed to persist storage state")

        # Close in reverse order of creation. Each step is individually
        # guarded so a failure in one does not prevent the others.
        self._safe_close(self.context, "browser context")
        self._safe_close(self.browser, "browser")
        self._safe_stop(self._playwright, "playwright driver")

        # Drop references so a stale attribute cannot be used after exit.
        self.context = None
        self.browser = None
        self._playwright = None

    def new_page(self) -> Page:
        """Open a new page with the project's default timeouts applied.

        The page inherits the context's locale, viewport and storage
        state. Timeouts are set here (not in the context) so that
        different pages could, in the future, use different budgets.
        """
        if self.context is None:
            raise RuntimeError("BrowserSession is not active. Use it as a context manager.")
        page = self.context.new_page()
        page.set_default_timeout(self.settings.default_timeout_ms)
        page.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        return page

    def mark_dirty(self) -> None:
        """Flag the in-memory storage state as needing to be persisted."""
        self._dirty = True

    def mark_logged_in(self) -> None:
        """Record a successful login and request state persistence.

        Starting the age counter here (rather than at process start)
        means :meth:`session_age_seconds` measures the session's actual
        lifetime, not the process's.
        """
        self.started_at = time.monotonic()
        self.mark_dirty()

    def session_age_seconds(self) -> float | None:
        """Seconds since the last successful login, or ``None`` if never."""
        if self.started_at is None:
            return None
        return time.monotonic() - self.started_at

    @staticmethod
    def _safe_close(resource: object | None, label: str) -> None:
        """Close a Playwright resource, tolerating a dead driver.

        The specific string check exists because Playwright's shutdown
        errors are indistinguishable from real failures by type alone:
        both surface as generic exceptions. We special-case the
        "already gone" message so it does not pollute the logs with a
        misleading stack trace.
        """
        if resource is None:
            return
        try:
            resource.close()
        except Exception as exc:
            if "Connection closed while reading from the driver" in str(exc):
                log.debug("Ignoring %s close failure during shutdown: %s", label, exc)
            else:
                log.debug("Error closing %s: %s", label, exc, exc_info=True)

    @staticmethod
    def _safe_stop(resource: object | None, label: str) -> None:
        """Stop a Playwright resource, tolerating a dead driver.

        Same rationale as :meth:`_safe_close`; kept separate because the
        two are called on different resource types and the labels help
        when reading logs.
        """
        if resource is None:
            return
        try:
            resource.stop()
        except Exception as exc:
            if "Connection closed while reading from the driver" in str(exc):
                log.debug("Ignoring %s stop failure during shutdown: %s", label, exc)
            else:
                log.debug("Error stopping %s: %s", label, exc, exc_info=True)
