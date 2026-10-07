"""DOM selectors for the banking portal, plus an optional YAML loader.

The portal's markup is the most volatile part of this project: a class
rename upstream breaks every locator in one shot. To keep that pain
contained, *every* CSS string used by the bot lives here as a field of
the :class:`Selectors` dataclass. If the UI changes, this is the only
file you need to touch — or, better, override the affected keys from
``config/selectors.yaml`` without editing code at all.

The loader is deliberately forgiving:

* If PyYAML is not installed, we silently fall back to defaults.
* If the YAML is malformed or contains unknown keys, we log and ignore
  them rather than crash.

Rationale: a selector typo should degrade the bot to "uses the default
selector" (which may still work), not prevent it from starting.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import logging
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    # ``Path`` only appears in annotations; ``from __future__ import
    # annotations`` makes those lazy, so we can defer the import.
    from pathlib import Path

try:
    import yaml

    _HAS_YAML = True
except ImportError:
    # PyYAML is an optional dependency. ``load_selectors`` checks this
    # flag instead of re-importing, so the import cost is paid once.
    _HAS_YAML = False


@dataclass(frozen=True)
class Selectors:
    """Frozen container of every CSS selector used by the bot.

    Each field is a string that Playwright will pass verbatim to its
    locator engine. Fields are grouped by the page/flow they belong to,
    mirroring the sections in ``config/selectors.yaml``.

    The defaults match the portal as of the last known-good run. They
    are intentionally narrow (attribute-based, not positional) to
    survive cosmetic changes such as column reordering.

    Notes:
    -----
    Instances are immutable. To derive a modified copy — e.g. in a test
    that wants to simulate a UI rename — use :meth:`replace`.
    """

    # --- Login page -----------------------------------------------------
    username_input: str = 'input[formcontrolname="username"], input[aria-label="usuario"]'
    password_input: str = 'input[formcontrolname="password"], input[name="password"]'
    login_button: str = '.button-login-container button[type="submit"]'
    continue_button: str = '.button-container button[type="submit"]'

    # --- Accounts dashboard --------------------------------------------
    accounts_table: str = "table.table-saldo-cuenta"
    account_rows: str = "table.table-saldo-cuenta tbody tr"
    account_type_cell: str = "td[headers='cuenta']"
    account_number_cell: str = "td[headers='numero']"
    visibility_icon: str = 'mat-icon:has-text("visibility")'

    # --- Balance dialog (Angular Material) -----------------------------
    balance_dialog: str = "mat-dialog-container"
    balance_rows: str = "mat-dialog-container tbody tr"
    balance_diferido_cell: str = "td:nth-child(2)"
    balance_disponible_cell: str = "td:nth-child(3)"
    dialog_close_button: str = "mat-dialog-container button[mat-dialog-close]"

    def replace(self, **overrides: str) -> Selectors:
        """Return a new :class:`Selectors` with the given fields replaced.

        Thin wrapper around :func:`dataclasses.replace` so callers do not
        need to import it. Kept as a method (not a module function) so
        that ``settings.selectors.replace(...)`` reads naturally in
        tests.
        """
        return replace(self, **overrides)


def _flatten_yaml(data: dict[str, Any]) -> dict[str, str]:
    """Collapse nested YAML sections into a flat ``{field: selector}`` map.

    The YAML file is organized by section for readability::

        login:
          username_input: '...'
        accounts:
          accounts_table: '...'

    but :class:`Selectors` is flat. This function bridges the two. It
    also tolerates a flat YAML (``username_input: '...'`` at the top
    level) so users can write either style.

    Non-string leaves are logged and dropped: a selector must be a
    string, and silently coercing e.g. an int to ``"5"`` would produce a
    confusing failure downstream.
    """
    flat: dict[str, str] = {}
    for section, value in data.items():
        if isinstance(value, dict):
            for key, val in value.items():
                if not isinstance(val, str):
                    log.warning(
                        "Selector %s.%s is not a string; ignoring.",
                        section,
                        key,
                    )
                    continue
                flat[key] = val
        elif isinstance(value, str):
            # Support flat YAML files for users who prefer one level.
            flat[section] = value
        else:
            log.warning("Ignoring unrecognized selector entry %r.", section)
    return flat


def load_selectors(path: Path | None = None) -> Selectors:
    """Build a :class:`Selectors` from an optional YAML override file.

    Parameters
    ----------
    path : Path | None
        Location of the YAML file. If ``None``, the path does not exist,
        or PyYAML is not installed, the defaults are returned unchanged.

    Returns:
    -------
    Selectors
        A new instance where every key present in the YAML replaces the
        corresponding default. Unknown keys are logged and skipped, so a
        stale YAML file never breaks startup.
    """
    if path is None or not path.exists():
        return Selectors()

    if not _HAS_YAML:
        log.warning(
            "PyYAML is not installed; ignoring %s and using default selectors.",
            path,
        )
        return Selectors()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        # Catching broadly here is intentional: a broken YAML should not
        # prevent the bot from starting with sane defaults.
        log.exception("Could not parse %s; using default selectors.", path)
        return Selectors()

    if not isinstance(raw, dict):
        log.warning("%s did not contain a mapping; using default selectors.", path)
        return Selectors()

    flat = _flatten_yaml(raw)
    known = {f.name for f in fields(Selectors)}

    # Warn on unknown keys rather than failing: this is how a typo in
    # selectors.yaml is surfaced without bringing the bot down.
    for key in flat:
        if key not in known:
            log.warning("Unknown selector %r in %s; ignoring.", key, path)

    overrides = {k: v for k, v in flat.items() if k in known}
    if overrides:
        log.info("Loaded %d selector override(s) from %s.", len(overrides), path)
    return Selectors(**overrides)
