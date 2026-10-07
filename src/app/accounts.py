from __future__ import annotations

from dataclasses import dataclass
import logging

from playwright.sync_api import Page, TimeoutError as PWTimeout

from app import config
from app.errors import ScrapingError

log = logging.getLogger(__name__)


_UNKNOWN = "N/A"


@dataclass(frozen=True)
class AccountBalance:
    account_type: str
    account_number: str
    deferred_balance: str
    available_balance: str


def _clean(text: str) -> str:
    cleaned = " ".join(text.split()).strip()
    return cleaned if cleaned else _UNKNOWN


def _extract_dialog_balance(
    page: Page,
    settings: config.Settings,
) -> tuple[str, str]:
    s = settings.selectors
    try:
        page.wait_for_selector(s.balance_rows)
    except PWTimeout as exc:
        raise ScrapingError("Balance dialog did not show any rows.") from exc

    rows = page.locator(s.balance_rows)
    if rows.count() == 0:
        log.warning("Balance dialog opened but has no rows.")
        return (_UNKNOWN, _UNKNOWN)

    first = rows.first
    deferred = _clean(first.locator(s.balance_diferido_cell).inner_text())
    available = _clean(first.locator(s.balance_disponible_cell).inner_text())
    return (deferred, available)


def _close_dialog(page: Page, settings: config.Settings) -> None:
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
        row.locator(s.visibility_icon).first.click()

        try:
            deferred, available = _extract_dialog_balance(page, settings)
        except ScrapingError as exc:
            log.warning("Could not read balance dialog for %s: %s", account_number, exc)
            deferred, available = (_UNKNOWN, _UNKNOWN)
        except Exception:
            log.exception("Unexpected error reading dialog for %s.", account_number)
            deferred, available = (_UNKNOWN, _UNKNOWN)
        finally:
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
    print(format_balances(accounts))


__all__ = [
    "AccountBalance",
    "fetch_balances",
    "format_balances",
    "print_balances",
]
