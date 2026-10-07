"""Exception hierarchy for the banking bot.

The hierarchy is intentionally shallow: every error raised by this
package derives from :class:`BotError`, so callers can use a single
``except BotError`` as a catch-all when they do not care about the
specific failure mode.

Two subclasses add context that is useful for debugging but is *not*
part of the exception message:

* :class:`LoginError` carries the login ``step`` that failed and the URL
  at the moment of failure. ``main.py`` logs both.
* :class:`ScrapingError` carries the URL and the account identifier
  being processed when the failure happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # ``Path`` is only used in type hints. Importing it under
    # ``TYPE_CHECKING`` avoids a runtime import for what is a
    # zero-cost annotation with ``from __future__ import annotations``.
    from pathlib import Path


class BotError(Exception):
    """Base class for every recoverable error raised by this package.

    Catching this is the recommended way to handle "something went wrong
    but the process can keep running" scenarios (e.g. a single failed
    cycle). Fatal errors such as :class:`ConfigError` are *not*
    subclasses: they indicate the process cannot start at all.
    """


class LoginError(BotError):
    """Raised when the login flow cannot be completed.

    Attributes:
    ----------
    step : str | None
        Name of the login phase that failed. Current values are
        ``"navigate"``, ``"username"``, ``"password"``, ``"dashboard"``
        and ``"accounts"``. ``None`` if the failure happened outside the
        flow (e.g. reading an error banner).
    url : str | None
        Page URL at the moment of failure. Useful because the portal
        sometimes redirects to a maintenance page before timing out.
    debug_dir : Path | None
        Directory where the caller wrote the screenshot / HTML / URL
        artifacts for post-mortem analysis. ``None`` if the write
        itself failed.
    """

    def __init__(
        self,
        message: str,
        *,
        step: str | None = None,
        url: str | None = None,
        debug_dir: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.url = url
        self.debug_dir = debug_dir


class ScrapingError(BotError):
    """Raised when account data cannot be read from the dashboard.

    This is *not* raised for individual rows that fail to open their
    dialog: those are reported as ``"N/A"`` in the result list. It is
    reserved for failures that prevent the scrape as a whole (e.g. the
    accounts table never appears).

    Attributes:
    ----------
    url : str | None
        Page URL at the moment of failure.
    account : str | None
        Account identifier being processed, when applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        account: str | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.account = account
