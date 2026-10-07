"""Entry point, main loop and process-level concerns.

This module owns everything that is not specific to the portal:

* CLI parsing (:func:`_parse_args`).
* Startup validation (:func:`_bootstrap`).
* Signal handling and graceful shutdown
  (:func:`_handle_shutdown_signal`).
* The cycle loop (:func:`_run_loop`) and its back-off
  (:func:`_backoff_delay`).
* Result persistence (:func:`_save_results`).
* A best-effort network probe (:func:`_network_ok`).

The portal-specific work (browser, login, scraping) lives in sibling
modules; this file only sequences them.

Global state
------------
``_running`` and ``_force_exit`` are module-level flags mutated by the
signal handler and read by the loop. They exist because Python signal
handlers can only communicate with the main thread through globals.
The roadmap tracks replacing them with a ``threading.Event`` from an
``app.runtime`` module; until then, treat them as private and do not
read them from other modules (``keepalive`` currently does — that is
the one known exception).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import logging
import os
import random
import signal
import sys
import time
from typing import TYPE_CHECKING
import urllib.error
import urllib.request

from playwright.sync_api import TimeoutError as PWTimeout

from app import accounts as acc_module, config
from app.browser import BrowserSession
from app.config import setup_logging
from app.config.logging import shutdown_logging
from app.errors import LoginError, ScrapingError
from app.login import ensure_logged_in

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

log = logging.getLogger(__name__)


# How many consecutive cycle failures before we give up entirely.
_MAX_NETWORK_FAILURES = 10

# Exponential back-off parameters. Delay = BASE * 2^(failures-1), capped
# at CAP, then multiplied by a jitter factor in [0.5, 1.0].
_BACKOFF_BASE = 15
_BACKOFF_CAP = 600

# Timeout for the lightweight network probe (see :func:`_network_ok`).
_NETWORK_PROBE_TIMEOUT = 5.0

_running = True
_force_exit = False


def _handle_shutdown_signal(signum: int, _frame: object) -> None:
    """Handle SIGINT/SIGTERM with a two-stage shutdown.

    First signal: request a graceful stop. The current cycle finishes,
    then :func:`_run_loop` exits normally. This is important because
    interrupting mid-cycle can leave ``state.json`` half-written and the
    Playwright driver in an inconsistent state.

    Second signal: force immediate exit. The user is assumed to know
    what they are doing (or is impatient), so we flush logs and call
    ``os._exit`` with the conventional "terminated by SIGINT" code.

    ``_force_exit`` is checked *before* ``_running`` so that a second
    signal does not get absorbed by the first branch after the first
    signal has already set ``_running = False``.
    """
    global _running, _force_exit
    name = signal.Signals(signum).name

    if _force_exit:
        return

    if _running:
        log.info("Received %s, will shut down after the current cycle.", name)
        _running = False
        return

    _force_exit = True
    log.warning("Received %s again, forcing immediate exit.", name)
    shutdown_logging()
    os._exit(130)


def _save_results(
    accounts: list[acc_module.AccountBalance],
    out_dir: Path,
) -> Path | None:
    """Persist one cycle's results as a timestamped JSON file.

    Returns the path on success, or ``None`` if the write failed. The
    caller treats persistence as best-effort: a failed write is logged
    but does not abort the cycle, because the balances have already been
    printed and the next cycle will produce a new snapshot anyway.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"balances_{timestamp}.json"
        payload = {
            # ``schema_version`` lets downstream consumers detect format
            # changes without inspecting the payload structure.
            "schema_version": 1,
            "timestamp": timestamp,
            "accounts": [asdict(acc) for acc in accounts],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.debug("Results saved to %s", path)
        return path
    except Exception:
        log.exception("Failed to save results to %s", out_dir)
        return None


def _network_ok(url: str, timeout: float = _NETWORK_PROBE_TIMEOUT) -> bool:
    """Return ``True`` if ``url`` is reachable with a HEAD request.

    Any status code below 500 is considered "the network is fine": a
    4xx from a bank portal is normal and does not mean we should skip a
    login attempt. Only connection-level errors and 5xx responses
    indicate a problem worth postponing work for.

    This is a deliberately lightweight check, used as a gate before
    attempting a login after a suspected session death, to avoid a
    futile login attempt against an unreachable server.
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as exc:
        # HTTPError still carries a status code; treat 4xx as "server is
        # up and responding", 5xx as "server is up but unhappy".
        return 200 <= exc.code < 500
    except Exception as exc:
        # Connection refused, DNS failure, timeout… all treated the
        # same way: network problem.
        log.debug("Network probe to %s failed: %s", url, exc)
        return False


def _scrape_once(page: Page, session: BrowserSession) -> tuple[bool, int]:
    """Navigate to the accounts page and scrape one cycle's balances.

    Returns:
    -------
    tuple[bool, int]
        ``(success, account_count)``. ``success`` is ``False`` if any
        of the navigation or scraping steps failed; the count is 0 in
        that case. The caller uses the boolean to decide whether to
        reset the failure counter and the count purely for logging.
    """
    settings = session.settings

    try:
        page.goto(
            settings.accounts_url,
            wait_until="commit",
            timeout=settings.navigation_timeout_ms,
        )
    except Exception as exc:
        # Any failure to reach the accounts page counts as a network
        # failure: the caller will back off and retry.
        log.warning("Navigation before scrape failed: %s", exc)
        return (False, 0)

    try:
        balances = acc_module.fetch_balances(page, settings)
    except ScrapingError as exc:
        # The table was never visible: most likely a stale session. The
        # caller will retry, and a fresh login will be attempted on the
        # next cycle if the probe still fails.
        log.error("Scraping failed: %s", exc)
        return (False, 0)
    except PWTimeout as exc:
        log.error("Scraping timed out: %s", exc)
        return (False, 0)
    except Exception:
        # Catch-all so that a single misbehaving page does not kill the
        # process. The stack trace is logged for debugging.
        log.exception("Unexpected error while scraping.")
        return (False, 0)

    acc_module.print_balances(balances)
    _save_results(balances, settings.results_dir)
    return (True, len(balances))


def _backoff_delay(failures: int, rng: random.Random | None = None) -> float:
    """Return the delay in seconds before the next retry.

    Uses exponential back-off with full jitter in the lower half:
    ``delay = min(CAP, BASE * 2^(failures-1)) * U[0.5, 1.0]``.

    The jitter is intentional: it prevents a fleet of bots from
    synchronising their retries against the same server. ``rng`` is
    injectable so tests can make the result deterministic.
    """
    r = rng if rng is not None else random
    exponent = max(0, failures - 1)
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2**exponent))
    return delay * (0.5 + r.random() * 0.5)


def _interruptible_sleep(seconds: float) -> None:
    """Sleep for ``seconds``, checking the shutdown flag every second.

    A plain ``time.sleep`` would delay shutdown by up to the full
    duration. This loop keeps the response time under one second at the
    cost of a small busy-ish loop. Same roadmap item as in
    ``keepalive.sleep_with_keepalive``: replace with
    ``threading.Event.wait`` when the runtime refactor lands.
    """
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


def _bootstrap() -> int:
    """Configure logging and validate the environment before running.

    Returns:
    -------
    int
        ``0`` on success, non-zero on a fatal configuration error. The
        caller turns this into the process's exit code.

    Side effects
    ------------
    * Calls :func:`setup_logging` (idempotent).
    * Calls :meth:`Settings.ensure_paths` to create directories.
    * Logs the resolved configuration at INFO level so the startup log
      is a faithful record of how the process was configured.
    """
    settings = config.settings

    setup_logging(
        level=settings.log_level,
        log_file=settings.log_file,
        fmt=settings.log_format,
    )

    try:
        settings.validate()
        settings.ensure_paths()
    except config.ConfigError as exc:
        # Logging is already configured, so the operator will see this
        # on stdout even in a container.
        log.error("%s", exc)
        return 1

    # Non-fatal warnings: things that are legal but suspicious. Logged
    # here (not in Settings) so they appear after the logger is ready.
    for warning in settings.warnings():
        log.warning("%s", warning)

    log.info("Starting the Banking Bot.")
    log.info("Interval : %ss", settings.check_interval)
    log.info("Headless : %s", settings.headless)
    log.info("Slow-mo  : %sms", settings.slow_mo)
    log.info("Log level: %s (%s)", settings.log_level, settings.log_format)
    if settings.log_file:
        log.info("Log file : %s", settings.log_file)
    log.info("State    : %s", settings.storage_state)
    log.info("Results  : %s", settings.results_dir)
    log.info("Debug    : %s (keep %d)", settings.debug_dir, settings.debug_keep)

    return 0


def _run_loop(once: bool = False) -> int:
    """Run cycles until shutdown, back-off or success in ``once`` mode.

    The loop state machine per iteration is:

    1. If the previous iteration flagged the session as dead, and the
       network is reachable, log in directly (skipping the probe).
       If the network is not reachable, count a network failure without
       attempting a login.
    2. Otherwise, ensure a session (probe + maybe login).
    3. If login succeeded, scrape; otherwise count a network failure.
    4. If we have accumulated too many consecutive failures, stop.
    5. If not in ``once`` mode, sleep — either a short back-off if we
       just failed, or the full keepalive sleep.

    Parameters
    ----------
    once : bool, optional
        If ``True``, run exactly one cycle and return without sleeping.

    Returns:
    -------
    int
        ``0`` on clean exit, ``1`` if the failure threshold was reached.
    """
    # Imported lazily to avoid a circular import: ``keepalive`` imports
    # ``_running`` from this module at top level.
    from app.keepalive import KeepaliveResult, sleep_with_keepalive

    settings = config.settings
    network_failures = 0

    # Set to True after a keepalive reports the session as dead, so the
    # next iteration skips the probe (which would just confirm the same
    # thing) and goes straight to login.
    skip_next_session_check = False

    with BrowserSession(settings) as session:
        page = session.new_page()
        try:
            while _running:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log.info("=== Cycle started at %s ===", timestamp)
                cycle_start = time.monotonic()

                ok = False
                n_accounts = 0

                # --- (1) Login decision ------------------------------------
                if skip_next_session_check and not _network_ok(settings.login_url):
                    # Session is dead and the network is down: do not
                    # waste a login attempt. Count it and move on.
                    log.warning(
                        "Session was flagged as dead but the network is "
                        "unreachable; not attempting login."
                    )
                    network_failures += 1
                    skip_next_session_check = False
                else:
                    try:
                        ensure_logged_in(
                            page,
                            session,
                            skip_session_check=skip_next_session_check,
                        )
                    except LoginError as exc:
                        # Logging includes the step that failed; the
                        # debug artifacts (if any) are already on disk.
                        log.error("Login failed: %s (step=%s)", exc, exc.step)
                        network_failures += 1
                    else:
                        # --- (2) Scrape ------------------------------------
                        ok, n_accounts = _scrape_once(page, session)
                        if ok:
                            network_failures = 0
                        else:
                            network_failures += 1
                    finally:
                        # Reset regardless of outcome: the flag was
                        # consumed by this iteration.
                        skip_next_session_check = False

                elapsed = time.monotonic() - cycle_start

                # --- (3) Report -------------------------------------------
                if ok:
                    log.info(
                        "Cycle finished successfully (%d account(s), %.1fs).",
                        n_accounts,
                        elapsed,
                    )
                else:
                    log.warning(
                        "Cycle failed (%d consecutive, %.1fs).",
                        network_failures,
                        elapsed,
                    )

                # --- (4) Failure threshold ---------------------------------
                if network_failures >= _MAX_NETWORK_FAILURES:
                    log.error(
                        "Reached %d consecutive network failures. Stopping.",
                        _MAX_NETWORK_FAILURES,
                    )
                    return 1

                # --- (5) Exit / sleep --------------------------------------
                if once or not _running:
                    break

                if network_failures > 0:
                    delay = _backoff_delay(network_failures)
                    log.info(
                        "Backing off for %.1fs after %d consecutive failure(s).",
                        delay,
                        network_failures,
                    )
                    _interruptible_sleep(delay)
                    # Skip the keepalive sleep entirely after a failure:
                    # back-off is the whole point.
                    continue

                result = sleep_with_keepalive(
                    page,
                    session,
                    settings.check_interval,
                )

                if result is KeepaliveResult.DEAD:
                    log.warning(
                        "Session appears dead after keepalive. Skipping probe on the next cycle."
                    )
                    skip_next_session_check = True
                elif result is KeepaliveResult.NETWORK_ERROR:
                    log.warning(
                        "Keepalive hit repeated network errors. "
                        "Not re-logging in; counting as a network failure."
                    )
                    # Deliberately not setting skip_next_session_check:
                    # the session may still be alive, we just could not
                    # reach the server. Let the next cycle probe normally.
                    network_failures += 1
        finally:
            try:
                page.close()
            except Exception as exc:
                # The driver may already be gone if shutdown was forced.
                # Closing is best-effort in that case.
                if "Connection closed while reading from the driver" in str(exc):
                    log.debug("Page already closed (driver gone).")
                else:
                    log.debug("Error closing page: %s", exc, exc_info=True)

    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    The only current flag is ``--once``; more are on the roadmap
    (``--dry-run``, ``--accounts-only``). ``argv`` is injectable so
    tests can call this without touching ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        prog="banking-automation",
        description="Banking automation bot: logs in and scrapes account balances.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit. Useful for cron and CI.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns the process exit code."""
    args = _parse_args(argv)

    bootstrap_code = _bootstrap()
    if bootstrap_code != 0:
        return bootstrap_code

    # Install signal handlers *after* bootstrap so that a Ctrl+C during
    # early setup terminates with the default behaviour (a traceback)
    # rather than the graceful path, which needs the loop to be running.
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log.info("Shutdown handling: SIGTERM/SIGINT -> graceful stop (2nd signal forces exit).")
    if args.once:
        log.info("Running in --once mode.")

    return _run_loop(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
