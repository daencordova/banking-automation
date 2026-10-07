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
    from types import TracebackType

log = logging.getLogger(__name__)


class BrowserSession:
    def __init__(self, settings: config.Settings = config.settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self._dirty: bool = False
        self.started_at: float | None = None

    def __enter__(self) -> BrowserSession:
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=self.settings.headless,
            slow_mo=self.settings.slow_mo,
        )

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
        if self._dirty and self.context is not None:
            try:
                self.context.storage_state(path=str(self.settings.storage_state))
                if os.name == "posix":
                    os.chmod(self.settings.storage_state, 0o600)
                log.info("Session saved to %s", self.settings.storage_state)
            except Exception:
                log.exception("Failed to persist storage state")

        self._safe_close(self.context, "browser context")
        self._safe_close(self.browser, "browser")
        self._safe_stop(self._playwright, "playwright driver")

        self.context = None
        self.browser = None
        self._playwright = None

    def new_page(self) -> Page:
        if self.context is None:
            raise RuntimeError("BrowserSession is not active. Use it as a context manager.")
        page = self.context.new_page()
        page.set_default_timeout(self.settings.default_timeout_ms)
        page.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        return page

    def mark_dirty(self) -> None:
        self._dirty = True

    def mark_logged_in(self) -> None:
        self.started_at = time.monotonic()
        self.mark_dirty()

    def session_age_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return time.monotonic() - self.started_at

    @staticmethod
    def _safe_close(resource: object | None, label: str) -> None:
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
        if resource is None:
            return
        try:
            resource.stop()
        except Exception as exc:
            if "Connection closed while reading from the driver" in str(exc):
                log.debug("Ignoring %s stop failure during shutdown: %s", label, exc)
            else:
                log.debug("Error stopping %s: %s", label, exc, exc_info=True)
