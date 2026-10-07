from __future__ import annotations

from dataclasses import dataclass, fields, replace
import logging
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

try:
    import yaml

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


@dataclass(frozen=True)
class Selectors:
    username_input: str = 'input[formcontrolname="username"], input[aria-label="usuario"]'
    password_input: str = 'input[formcontrolname="password"], input[name="password"]'
    login_button: str = '.button-login-container button[type="submit"]'
    continue_button: str = '.button-container button[type="submit"]'

    accounts_table: str = "table.table-saldo-cuenta"
    account_rows: str = "table.table-saldo-cuenta tbody tr"
    account_type_cell: str = "td[headers='cuenta']"
    account_number_cell: str = "td[headers='numero']"
    visibility_icon: str = 'mat-icon:has-text("visibility")'

    balance_dialog: str = "mat-dialog-container"
    balance_rows: str = "mat-dialog-container tbody tr"
    balance_diferido_cell: str = "td:nth-child(2)"
    balance_disponible_cell: str = "td:nth-child(3)"
    dialog_close_button: str = "mat-dialog-container button[mat-dialog-close]"

    def replace(self, **overrides: str) -> Selectors:
        return replace(self, **overrides)


def _flatten_yaml(data: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}
    for section, value in data.items():
        if isinstance(value, dict):
            for key, val in value.items():
                if not isinstance(val, str):
                    log.warning("Selector %s.%s is not a string; ignoring.", section, key)
                    continue
                flat[key] = val
        elif isinstance(value, str):
            flat[section] = value
        else:
            log.warning("Ignoring unrecognized selector entry %r.", section)
    return flat


def load_selectors(path: Path | None = None) -> Selectors:
    if path is None or not path.exists():
        return Selectors()

    if not _HAS_YAML:
        log.warning("PyYAML is not installed; ignoring %s and using default selectors.", path)
        return Selectors()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("Could not parse %s; using default selectors.", path)
        return Selectors()

    if not isinstance(raw, dict):
        log.warning("%s did not contain a mapping; using default selectors.", path)
        return Selectors()

    flat = _flatten_yaml(raw)
    known = {f.name for f in fields(Selectors)}

    for key in flat:
        if key not in known:
            log.warning("Unknown selector %r in %s; ignoring.", key, path)

    overrides = {k: v for k, v in flat.items() if k in known}
    if overrides:
        log.info("Loaded %d selector override(s) from %s.", len(overrides), path)
    return Selectors(**overrides)
