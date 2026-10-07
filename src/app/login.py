from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING

from playwright.sync_api import Page, TimeoutError as PWTimeout

from app.errors import LoginError

if TYPE_CHECKING:
    from pathlib import Path

    from app import config
    from app.browser import BrowserSession

log = logging.getLogger(__name__)


def _prune_debug_artifacts(debug_dir: Path, keep: int) -> None:
    if keep <= 0 or not debug_dir.exists():
        return

    groups: dict[str, list[Path]] = {}
    for path in debug_dir.glob("login_fail_*"):
        groups.setdefault(path.stem, []).append(path)

    if len(groups) <= keep:
        return

    ordered = sorted(
        groups.items(),
        key=lambda item: max(p.stat().st_mtime for p in item[1]),
    )
    for _, files in ordered[:-keep]:
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.debug("Could not prune %s", path, exc_info=True)


def is_session_valid(page: Page, settings: config.Settings) -> bool:
    try:
        page.wait_for_selector(
            settings.selectors.accounts_table,
            timeout=settings.session_probe_timeout_ms,
        )
        return True
    except PWTimeout:
        return False


_LOGIN_ERROR_SELECTORS: tuple[str, ...] = (
    "text=Usuario o clave incorrectos",
    "text=Usuario o contraseña incorrectos",
    "text=Credenciales inválidas",
    "text=Datos incorrectos",
    ".error-message",
    ".mat-error",
)


def _read_login_error(page: Page) -> str | None:
    combined = ", ".join(_LOGIN_ERROR_SELECTORS)
    try:
        loc = page.locator(combined).first
        if loc.count() > 0:
            text = loc.inner_text().strip()
            if text:
                return text
    except Exception:
        log.debug("Could not read login error from page.", exc_info=True)
    return None


def _dump_debug_artifacts(page: Page, step: str, debug_dir: Path, keep: int) -> Path | None:
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = debug_dir / f"login_fail_{ts}_{step}"

        try:
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            log.debug("Could not capture screenshot.", exc_info=True)

        try:
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        except Exception:
            log.debug("Could not capture HTML.", exc_info=True)

        try:
            base.with_suffix(".url").write_text(page.url, encoding="utf-8")
        except Exception:
            log.debug("Could not capture URL.", exc_info=True)

        log.error("Debug artifacts written to %s.*", base)
        _prune_debug_artifacts(debug_dir, keep)
        return debug_dir
    except Exception:
        log.exception("Failed to write debug artifacts.")
        return None


def _invalidate_dom(page: Page, settings: config.Settings) -> None:
    try:
        page.wait_for_selector(
            settings.selectors.accounts_table,
            state="detached",
            timeout=2_000,
        )
    except PWTimeout:
        log.debug("Accounts table still attached before navigation.")


def login(page: Page, session: BrowserSession) -> None:
    """Perform the full login flow against the bank's portal."""
    settings = session.settings
    s = settings.selectors

    step = "navigate"

    try:
        log.info("Opening login page...")
        page.goto(
            settings.login_url,
            wait_until="commit",
            timeout=settings.navigation_timeout_ms,
        )

        step = "username"
        log.debug("Filling username...")
        page.wait_for_selector(s.username_input, timeout=settings.default_timeout_ms)
        page.fill(s.username_input, settings.username)
        page.click(s.login_button)

        step = "password"
        log.debug("Filling password...")
        page.wait_for_selector(s.password_input, timeout=settings.default_timeout_ms)
        page.fill(s.password_input, settings.password)
        page.click(s.continue_button)

        step = "dashboard"
        log.debug("Waiting for dashboard...")
        page.wait_for_url("**/main/**", timeout=settings.navigation_timeout_ms)

    except PWTimeout as exc:
        message = _read_login_error(page)
        debug_dir = _dump_debug_artifacts(page, step, settings.debug_dir, settings.debug_keep)
        if message:
            raise LoginError(
                f"Bank rejected login: {message}",
                step=step,
                url=page.url,
                debug_dir=debug_dir,
            ) from exc
        raise LoginError(
            f"Timed out during login flow at step: {step}",
            step=step,
            url=page.url,
            debug_dir=debug_dir,
        ) from exc

    step = "accounts"
    _invalidate_dom(page, settings)
    page.goto(
        settings.accounts_url,
        wait_until="commit",
        timeout=settings.navigation_timeout_ms,
    )
    if not is_session_valid(page, settings):
        message = _read_login_error(page)
        debug_dir = _dump_debug_artifacts(page, step, settings.debug_dir, settings.debug_keep)
        if message:
            raise LoginError(
                f"Bank rejected login: {message}",
                step=step,
                url=page.url,
                debug_dir=debug_dir,
            )
        raise LoginError(
            "Accounts table not visible after login.",
            step=step,
            url=page.url,
            debug_dir=debug_dir,
        )

    session.mark_logged_in()
    log.info("Login successful.")


def ensure_logged_in(
    page: Page,
    session: BrowserSession,
    *,
    skip_session_check: bool = False,
) -> None:
    settings = session.settings

    if skip_session_check:
        log.info("Skipping session check; previous keepalive flagged the session as dead.")
        login(page, session)
        return

    if is_session_valid(page, settings):
        log.debug("Session already valid on current page.")
        return

    if session.started_at is None and not settings.storage_state.exists():
        log.info("No saved session and no prior login; going straight to login.")
        login(page, session)
        return

    log.info("Checking for an existing session...")
    try:
        page.goto(
            settings.accounts_url,
            wait_until="commit",
            timeout=settings.session_check_timeout_ms,
        )

        if is_session_valid(page, settings):
            log.info("Existing session is still valid.")
            session.started_at = time.monotonic()
            return
    except PWTimeout:
        log.debug("No valid session found (timeout). Will log in.")
    except Exception as exc:
        log.debug("Existing-session check failed: %s", exc, exc_info=True)

    age = session.session_age_seconds()
    if age is not None:
        log.warning("Session died after %.1fs of life.", age)

    log.info("Session expired or missing, performing full login.")
    login(page, session)
