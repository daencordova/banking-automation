"""Login flow and session probing.

This module answers a single question — "is the current page a valid
logged-in session?" — and, when the answer is no, performs the full
login flow.

The flow is intentionally split from :mod:`app.browser` and
:mod:`app.accounts` so that the state machine ("probe → maybe login →
verify") is readable in one place. Callers use :func:`ensure_logged_in`
and never need to know how a session is obtained, only that it is.

Debug artifacts (screenshot + HTML + URL) are written on login failure.
They are the difference between "the bot failed at 03:00" and "the bot
failed at 03:00 *because the portal showed a maintenance banner*".
"""

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
    """Delete old debug artifact groups, keeping the ``keep`` most recent.

    A "group" is every file sharing the same base name (``.png``,
    ``.html``, ``.url``) from a single failed run. Grouping by stem
    ensures we delete the screenshot and its companions together, never
    leaving orphans.

    A ``keep`` of 0 or less disables pruning.
    """
    if keep <= 0 or not debug_dir.exists():
        return

    groups: dict[str, list[Path]] = {}
    for path in debug_dir.glob("login_fail_*"):
        groups.setdefault(path.stem, []).append(path)

    if len(groups) <= keep:
        return

    # Sort by most recent file in each group, oldest first.
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
    """Return ``True`` if the accounts table is visible on the current page.

    This is the canonical "am I logged in?" check. It has no side
    effects beyond waiting up to ``session_probe_timeout_ms``: no
    navigation, no clicks. Callers are expected to have already
    navigated to a URL where the table would appear if the session were
    valid.
    """
    try:
        page.wait_for_selector(
            settings.selectors.accounts_table,
            timeout=settings.session_probe_timeout_ms,
        )
        return True
    except PWTimeout:
        return False


# Text markers the portal uses to indicate bad credentials. Kept as a
# tuple (not a set) so the join order in ``_read_login_error`` is stable
# and easier to diff when the portal changes wording.
_LOGIN_ERROR_SELECTORS: tuple[str, ...] = (
    "text=Usuario o clave incorrectos",
    "text=Usuario o contraseña incorrectos",
    "text=Credenciales inválidas",
    "text=Datos incorrectos",
    ".error-message",
    ".mat-error",
)


def _read_login_error(page: Page) -> str | None:
    """Return the visible login-error text, if any.

    Best-effort: any failure to locate or read the text returns ``None``
    so the caller can fall back to a generic "login failed" message.
    """
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
    """Write screenshot, HTML and URL for a failed login attempt.

    Each artifact is written independently: a failure to capture the
    screenshot must not prevent the HTML dump, and vice versa. The
    function returns the directory on success (for logging) or ``None``
    if the whole operation failed.
    """
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
    """Wait for the accounts table to detach before navigating away.

    Angular sometimes reuses the same DOM node across route changes. If
    we navigate to the accounts URL while the old table is still
    attached, a subsequent ``wait_for_selector`` may match the *stale*
    element and falsely conclude that login succeeded. Waiting for
    detachment removes that ambiguity. The wait is short and its failure
    is non-fatal.
    """
    try:
        page.wait_for_selector(
            settings.selectors.accounts_table,
            state="detached",
            timeout=2_000,
        )
    except PWTimeout:
        log.debug("Accounts table still attached before navigation.")


def login(page: Page, session: BrowserSession) -> None:
    """Perform the full login flow against the bank's portal.

    The flow is:

    1. Navigate to the login page.
    2. Fill the username and click the "continue" button.
    3. Fill the password and click the "submit" button.
    4. Wait for a URL under ``/main/``.
    5. Verify the accounts table is actually present.

    Step 5 is not redundant: the portal occasionally redirects to
    ``/main/`` even when authentication failed (e.g. a maintenance page
    under the same prefix), so checking the URL alone is not sufficient.

    On any timeout, a set of debug artifacts is written and a
    :class:`LoginError` is raised. The caller is responsible for
    retry/backoff decisions; this function does not retry.
    """
    settings = session.settings
    s = settings.selectors

    # ``step`` is updated as we progress so that the error we raise on
    # timeout points at the exact phase that failed. It is included in
    # the LoginError and in the debug artifact filename.
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
        # Try to read a portal-provided error message first; it is more
        # actionable than "timed out at step X".
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

    # Only now is the session considered usable. This call marks the
    # session dirty so the storage state is persisted on exit.
    session.mark_logged_in()
    log.info("Login successful.")


def ensure_logged_in(
    page: Page,
    session: BrowserSession,
    *,
    skip_session_check: bool = False,
) -> None:
    """Guarantee that ``page`` is on a logged-in session, logging in if needed.

    Parameters
    ----------
    page : Page
        A page belonging to ``session``. Its URL is not assumed.
    session : BrowserSession
        Used to read settings and to mark the session dirty on success.
    skip_session_check : bool, optional
        If ``True``, bypass the "is there a valid session?" probe and go
        straight to login. Used by the main loop after a keepalive
        failure, where the session is already known to be dead and the
        probe would just waste time.

    Notes:
    -----
    The decision tree is deliberately explicit rather than clever:

    * If the caller says the session is dead → login.
    * Else if the *current page* is already valid → return.
    * Else if we have never logged in and have no saved state → login.
    * Else probe the accounts URL; if valid, adopt it; else login.
    """
    settings = session.settings

    if skip_session_check:
        log.info("Skipping session check; previous keepalive flagged the session as dead.")
        login(page, session)
        return

    if is_session_valid(page, settings):
        log.debug("Session already valid on current page.")
        return

    # No prior login in this process and no state file to restore from:
    # probing would just navigate to the login page and fail the check.
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
            # Adopt the restored session: record its start time so
            # ``session_age_seconds`` is meaningful later, but do NOT
            # mark it dirty — nothing changed, and a rewrite could
            # clobber a good state file with an expired one.
            session.started_at = time.monotonic()
            return
    except PWTimeout:
        log.debug("No valid session found (timeout). Will log in.")
    except Exception as exc:
        log.debug("Existing-session check failed: %s", exc, exc_info=True)

    # If we had a start time, the session died mid-life; that is worth
    # a warning because it may indicate a shorter-than-expected server
    # timeout.
    age = session.session_age_seconds()
    if age is not None:
        log.warning("Session died after %.1fs of life.", age)

    log.info("Session expired or missing, performing full login.")
    login(page, session)
