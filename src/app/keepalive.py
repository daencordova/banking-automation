"""Session keepalive during the long sleep between cycles.

The bank's session expires after a period of inactivity. Since the bot
sleeps for ``CHECK_INTERVAL`` seconds (default 300) between cycles,
letting the session idle for that long would force a fresh login on
almost every cycle. To avoid that, this module reloads the accounts URL
periodically during the sleep, refreshing the session server-side.

Two behaviours are worth calling out:

* **Network errors vs dead session.** A failed reload can mean either
  "the session is gone" (needs re-login) or "the network hiccuped"
  (needs patience). The two are distinguished by looking at the
  resulting URL: if we do not land on ``/main/``, the session is
  considered dead. Transient network failures are counted and, after a
  few in a row, reported as such.
* **Cooperative shutdown.** The sleep loop checks the running flag
  frequently so a SIGTERM is honoured within a second, not after the
  full sleep.

NOTE: this module currently imports the running flag from
:mod:`app.main`. That is a known wart (implicit circular dependency and
a mutable global read from another module). The roadmap tracks moving
it to a dedicated ``app.runtime`` module backed by a
``threading.Event``. Do not add more dependencies on ``app.main``
here in the meantime.
"""

from __future__ import annotations

from enum import Enum
import logging
import random
import time
from typing import TYPE_CHECKING

from app.main import _running

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from app.browser import BrowserSession

log = logging.getLogger(__name__)


# Bounds for the interval between keepalive reloads. The actual value is
# derived from ``CHECK_INTERVAL`` (roughly one third) and clamped here.
_KEEPALIVE_MIN = 60
_KEEPALIVE_MAX = 120


class KeepaliveResult(Enum):
    """Outcome of a keepalive attempt, consumed by the main loop.

    ``ALIVE`` and ``DEAD`` are self-explanatory. ``NETWORK_ERROR`` is
    deliberately distinct: the caller treats it as a network failure
    (back-off) rather than a session failure (re-login), because
    re-logging in will not help if the portal is unreachable.
    """

    ALIVE = "alive"
    DEAD = "dead"
    NETWORK_ERROR = "network_error"


def keepalive_refresh(page: Page, session: BrowserSession) -> KeepaliveResult:
    """Reload the accounts URL and classify the result.

    Uses ``wait_until="commit"`` rather than ``"load"`` or
    ``"networkidle"``: we only need the response to have started, and
    the follow-up selector wait is a stronger signal of readiness than
    any load event.
    """
    settings = session.settings

    try:
        page.goto(
            settings.accounts_url,
            wait_until="commit",
            timeout=settings.navigation_timeout_ms,
        )
    except Exception as exc:
        # Includes timeouts and Chromium-level errors. Both are
        # classified as network problems, not session death.
        log.warning("Keepalive navigation failed: %s", exc)
        return KeepaliveResult.NETWORK_ERROR

    # URL check first: a redirect to the login page is cheap to detect
    # and unambiguous. Only if we landed somewhere plausible do we pay
    # the cost of waiting for the table.
    if "/main/" not in page.url:
        log.warning("Keepalive landed on %s (not /main/). Session is dead.", page.url)
        return KeepaliveResult.DEAD

    try:
        page.wait_for_selector(
            settings.selectors.accounts_table,
            timeout=settings.session_probe_timeout_ms,
        )
    except Exception as exc:
        # We are on /main/ but the table is not there: most likely the
        # session expired mid-render. Treat as dead.
        log.warning("Keepalive probe failed: %s", exc)
        return KeepaliveResult.DEAD

    log.debug("Keepalive reload OK (url=%s).", page.url)
    return KeepaliveResult.ALIVE


def sleep_with_keepalive(
    page: Page,
    session: BrowserSession,
    seconds: int,
) -> KeepaliveResult:
    """Sleep for roughly ``seconds`` while refreshing the session periodically.

    The actual sleep includes up to 10 % jitter (capped at 30 s) so that
    multiple instances do not synchronise their requests if the bot is
    ever run in parallel.

    Parameters
    ----------
    page : Page
        Logged-in page, used for the periodic reload.
    session : BrowserSession
        Provides settings (notably timeouts).
    seconds : int
        Nominal sleep duration in seconds. The effective duration is
        slightly longer due to jitter.

    Returns:
    -------
    KeepaliveResult
        The last observed state:

        * ``ALIVE`` — sleep completed normally.
        * ``DEAD`` — session was detected dead, sleep aborted early.
        * ``NETWORK_ERROR`` — repeated network failures, sleep aborted
          early after three consecutive errors.
    """
    # Jitter: at most 10 % of the nominal interval, capped at 30 s.
    jitter = random.uniform(0, min(30, seconds * 0.1))
    total = seconds + jitter

    # Target roughly three keepalives per sleep, clamped to a sane range
    # so that very long or very short intervals still produce sensible
    # reload cadence.
    keepalive_every = max(_KEEPALIVE_MIN, min(_KEEPALIVE_MAX, seconds // 3))

    log.info(
        "Sleeping for %.1fs (keepalive every %ds)...",
        total,
        keepalive_every,
    )

    end = time.monotonic() + total
    next_keepalive = time.monotonic() + keepalive_every
    last_result = KeepaliveResult.ALIVE
    consecutive_network_errors = 0

    # Three consecutive network errors is the threshold at which we
    # stop trying: at that point the main loop's back-off is a better
    # strategy than continuing to hammer the portal.
    max_network_errors = 3

    while _running and time.monotonic() < end:
        now = time.monotonic()
        if now >= next_keepalive:
            last_result = keepalive_refresh(page, session)

            if last_result is KeepaliveResult.DEAD:
                log.warning("Session is dead. Aborting sleep early.")
                return KeepaliveResult.DEAD

            if last_result is KeepaliveResult.NETWORK_ERROR:
                consecutive_network_errors += 1
                if consecutive_network_errors >= max_network_errors:
                    log.error(
                        "Keepalive failed %d times in a row (network). Aborting sleep.",
                        consecutive_network_errors,
                    )
                    return KeepaliveResult.NETWORK_ERROR
            else:
                # A single success resets the counter: transient errors
                # should not accumulate across a long, otherwise healthy
                # sleep.
                consecutive_network_errors = 0

            next_keepalive = time.monotonic() + keepalive_every

        # Sleep in small increments so SIGTERM is honoured within ~1 s.
        # NOTE: this is a busy-ish loop. Replacing it with a
        # ``threading.Event.wait`` is on the roadmap (see module
        # docstring).
        remaining = end - time.monotonic()
        time.sleep(min(1.0, max(0.0, remaining)))

    return last_result
