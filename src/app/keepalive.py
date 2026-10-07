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


_KEEPALIVE_MIN = 60
_KEEPALIVE_MAX = 120


class KeepaliveResult(Enum):
    ALIVE = "alive"
    DEAD = "dead"
    NETWORK_ERROR = "network_error"


def keepalive_refresh(page: Page, session: BrowserSession) -> KeepaliveResult:
    settings = session.settings

    try:
        page.goto(
            settings.accounts_url,
            wait_until="commit",
            timeout=settings.navigation_timeout_ms,
        )
    except Exception as exc:
        log.warning("Keepalive navigation failed: %s", exc)
        return KeepaliveResult.NETWORK_ERROR

    if "/main/" not in page.url:
        log.warning("Keepalive landed on %s (not /main/). Session is dead.", page.url)
        return KeepaliveResult.DEAD

    try:
        page.wait_for_selector(
            settings.selectors.accounts_table,
            timeout=settings.session_probe_timeout_ms,
        )
    except Exception as exc:
        log.warning("Keepalive probe failed: %s", exc)
        return KeepaliveResult.DEAD

    log.debug("Keepalive reload OK (url=%s).", page.url)
    return KeepaliveResult.ALIVE


def sleep_with_keepalive(
    page: Page,
    session: BrowserSession,
    seconds: int,
) -> KeepaliveResult:
    jitter = random.uniform(0, min(30, seconds * 0.1))
    total = seconds + jitter

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
                consecutive_network_errors = 0

            next_keepalive = time.monotonic() + keepalive_every

        remaining = end - time.monotonic()
        time.sleep(min(1.0, max(0.0, remaining)))

    return last_result
