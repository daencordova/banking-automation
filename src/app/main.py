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


_MAX_NETWORK_FAILURES = 10
_BACKOFF_BASE = 15
_BACKOFF_CAP = 600
_NETWORK_PROBE_TIMEOUT = 5.0

_running = True
_force_exit = False


def _handle_shutdown_signal(signum: int, _frame: object) -> None:
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
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"balances_{timestamp}.json"
        payload = {
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
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 500
    except Exception as exc:
        log.debug("Network probe to %s failed: %s", url, exc)
        return False


def _scrape_once(page: Page, session: BrowserSession) -> tuple[bool, int]:
    settings = session.settings

    try:
        page.goto(
            settings.accounts_url,
            wait_until="commit",
            timeout=settings.navigation_timeout_ms,
        )
    except Exception as exc:
        log.warning("Navigation before scrape failed: %s", exc)
        return (False, 0)

    try:
        balances = acc_module.fetch_balances(page, settings)
    except ScrapingError as exc:
        log.error("Scraping failed: %s", exc)
        return (False, 0)
    except PWTimeout as exc:
        log.error("Scraping timed out: %s", exc)
        return (False, 0)
    except Exception:
        log.exception("Unexpected error while scraping.")
        return (False, 0)

    acc_module.print_balances(balances)
    _save_results(balances, settings.results_dir)
    return (True, len(balances))


def _backoff_delay(failures: int, rng: random.Random | None = None) -> float:
    r = rng if rng is not None else random
    exponent = max(0, failures - 1)
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2**exponent))
    return delay * (0.5 + r.random() * 0.5)


def _interruptible_sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


def _bootstrap() -> int:
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
        log.error("%s", exc)
        return 1

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
    from app.keepalive import KeepaliveResult, sleep_with_keepalive

    settings = config.settings
    network_failures = 0
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

                if skip_next_session_check and not _network_ok(settings.login_url):
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
                        log.error("Login failed: %s (step=%s)", exc, exc.step)
                        network_failures += 1
                    else:
                        ok, n_accounts = _scrape_once(page, session)
                        if ok:
                            network_failures = 0
                        else:
                            network_failures += 1
                    finally:
                        skip_next_session_check = False

                elapsed = time.monotonic() - cycle_start

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

                if network_failures >= _MAX_NETWORK_FAILURES:
                    log.error(
                        "Reached %d consecutive network failures. Stopping.",
                        _MAX_NETWORK_FAILURES,
                    )
                    return 1

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
                    network_failures += 1
        finally:
            try:
                page.close()
            except Exception as exc:
                if "Connection closed while reading from the driver" in str(exc):
                    log.debug("Page already closed (driver gone).")
                else:
                    log.debug("Error closing page: %s", exc, exc_info=True)

    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    args = _parse_args(argv)

    bootstrap_code = _bootstrap()
    if bootstrap_code != 0:
        return bootstrap_code

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log.info("Shutdown handling: SIGTERM/SIGINT -> graceful stop (2nd signal forces exit).")
    if args.once:
        log.info("Running in --once mode.")

    return _run_loop(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
