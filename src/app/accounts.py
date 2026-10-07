"""Reading account balances from the dashboard.

The dashboard shows one row per account. Each row has a "visibility"
icon that opens a Material dialog with the account's deferred and
available balances. There is no single page that lists every balance,
so a full read is necessarily a loop of "click icon → read dialog →
close dialog" per row.

The module is written to be resilient to a single bad row: if opening
or reading one dialog fails, that row is reported with ``"N/A"`` values
and the loop continues. A failure to find the accounts table itself is
fatal and raises :class:`ScrapingError`.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from playwright.sync_api import Page, TimeoutError as PWTimeout

from app import config
from app.errors import ScrapingError

log = logging.getLogger(__name__)


# Placeholder used whenever a value could not be read. Chosen over an
# empty string so that downstream consumers (JSON consumers, humans
# reading stdout) can distinguish "zero" from "unknown".
_UNKNOWN = "N/A"


@dataclass(frozen=True)
class AccountBalance:
    """A single account and its two balance figures.

    Balances are kept as raw strings (including currency and thousands
    separators) because the portal's formatting is authoritative and
    normalizing it here would lose information. Consumers that need
    numbers should parse them explicitly.

    Fields that could not be read are set to ``"N/A"``.
    """

    account_type: str
    account_number: str
    deferred_balance: str
    available_balance: str


def _clean(text: str) -> str:
    """Collapse whitespace and replace empty results with ``"N/A"``.

    Angular inserts line breaks and non-breaking spaces inside table
    cells. Collapsing to single spaces makes the output diff-stable
    between runs and easier to paste into other tools.
    """
    cleaned = " ".join(text.split()).strip()
    return cleaned if cleaned else _UNKNOWN


def _extract_dialog_balance(
    page: Page,
    settings: config.Settings,
) -> tuple[str, str]:
    """Read the deferred and available balances from an open dialog.

    Returns:
    -------
    tuple[str, str]
        ``(deferred, available)``. If the dialog has no rows (a state
        the portal sometimes reaches while loading), both values are
        ``"N/A"`` rather than an error, because the row itself is not
        considered failed.
    """
    s = settings.selectors
    try:
        page.wait_for_selector(s.balance_rows)
    except PWTimeout as exc:
        # The dialog opened but never populated. This is treated as a
        # failure by the caller, which catches ScrapingError per row.
        raise ScrapingError("Balance dialog did not show any rows.") from exc

    rows = page.locator(s.balance_rows)
    if rows.count() == 0:
        # An empty dialog is a valid, if useless, outcome: the account
        # simply has nothing to show. Not worth an exception.
        log.warning("Balance dialog opened but has no rows.")
        return (_UNKNOWN, _UNKNOWN)

    first = rows.first
    deferred = _clean(first.locator(s.balance_diferido_cell).inner_text())
    available = _clean(first.locator(s.balance_disponible_cell).inner_text())
    return (deferred, available)


def _close_dialog(page: Page, settings: config.Settings) -> None:
    """Close the balance dialog, with a keyboard fallback.

    The dialog is Angular Material and normally closes via its X button.
    Occasionally the button is covered by an overlay or the click lands
    before the dialog is interactive; in those cases Escape is the
    documented Material fallback. If even Escape fails, we log an error
    and move on — the next iteration will likely fail too, and the main
    loop's back-off will eventually recover.
    """
    s = settings.selectors

    try:
        page.click(s.dialog_close_button, timeout=2_000)
        page.wait_for_selector(s.balance_dialog, state="detached", timeout=3_000)
        return
    except PWTimeout:
        log.warning("Close button failed, pressing Escape.")

    try:
        page.keyboard.press("Escape")
        page.wait_for_selector(s.balance_dialog, state="detached", timeout=2_000)
    except PWTimeout:
        log.error("Balance dialog did not close; next iteration may fail.")


def fetch_balances(
    page: Page,
    settings: config.Settings = config.settings,
) -> list[AccountBalance]:
    """Read every account row and its balances.

    The caller is responsible for being on the accounts URL and for
    having a valid session; this function does not navigate.

    Parameters
    ----------
    page : Page
        Logged-in Playwright page positioned on the dashboard.
    settings : config.Settings, optional
        Used for selectors and timeout budgets.

    Returns:
    -------
    list[AccountBalance]
        One entry per row, in the order the portal renders them. Rows
        that fail to open or read their dialog are reported with
        ``"N/A"`` values rather than being skipped, so the caller can
        still see that the account exists.

    Raises:
    ------
    ScrapingError
        If the accounts table itself never becomes visible. This is
        fatal because it means we are not looking at the dashboard at
        all (expired session, maintenance page, selector drift).
    """
    s = settings.selectors

    log.info("Reading account table...")
    try:
        page.wait_for_selector(s.accounts_table)
    except PWTimeout as exc:
        raise ScrapingError(
            f"Accounts table not visible at {page.url}",
            url=page.url,
        ) from exc

    rows = page.locator(s.account_rows)
    total = rows.count()
    log.info("Found %d account(s).", total)

    results: list[AccountBalance] = []

    for i in range(total):
        row = rows.nth(i)
        account_type = _clean(row.locator(s.account_type_cell).inner_text())
        account_number = _clean(row.locator(s.account_number_cell).inner_text())

        log.info("Opening balance for %s (%s)...", account_number, account_type)
        # ``.first`` because a row may contain multiple icons; the
        # visibility icon is always the first one in the current markup.
        row.locator(s.visibility_icon).first.click()

        try:
            deferred, available = _extract_dialog_balance(page, settings)
        except ScrapingError as exc:
            log.warning("Could not read balance dialog for %s: %s", account_number, exc)
            deferred, available = (_UNKNOWN, _UNKNOWN)
        except Exception:
            # Catch-all: a single misbehaving row must not abort the
            # whole cycle. The stack trace is logged for debugging.
            log.exception("Unexpected error reading dialog for %s.", account_number)
            deferred, available = (_UNKNOWN, _UNKNOWN)
        finally:
            # The dialog must be closed before the next row's icon can
            # be clicked: it is modal and would intercept the click.
            _close_dialog(page, settings)

        results.append(
            AccountBalance(
                account_type=account_type,
                account_number=account_number,
                deferred_balance=deferred,
                available_balance=available,
            )
        )

    return results


def format_balances(accounts: list[AccountBalance]) -> str:
    """Render balances as the human-readable block printed each cycle.

    The format is intentionally stable: it is meant to be greppable and
    diffable, not pretty-printed for a terminal width.
    """
    lines = ["", "=" * 60, " Banking Account Balances", "=" * 60]

    if not accounts:
        lines.append(" No accounts found.")
    else:
        for acc in accounts:
            lines += [
                "",
                f" Account    : {acc.account_type}",
                f" Number     : {acc.account_number}",
                f" Deferred   : {acc.deferred_balance}",
                f" Available  : {acc.available_balance}",
            ]

    lines += ["", "=" * 60, ""]
    return "\n".join(lines)


def print_balances(accounts: list[AccountBalance]) -> None:
    """Print :func:`format_balances` output to stdout."""
    print(format_balances(accounts))


__all__ = [
    "AccountBalance",
    "fetch_balances",
    "format_balances",
    "print_balances",
]
