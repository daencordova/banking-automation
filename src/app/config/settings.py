"""Runtime configuration, loaded once from the environment.

Every environment variable the bot understands is defined and validated
here. The module deliberately centralizes three things that are easy to
get wrong:

1. **Parsing** — ``_env_str``, ``_env_int`` and ``_env_bool`` normalise
   missing/empty/invalid values into predictable behaviour.
2. **Validation** — :meth:`Settings.validate` collects *all* problems
   instead of raising on the first one, so a misconfigured deployment
   learns about every issue in a single run.
3. **Side effects** — ``load_dotenv()`` runs at import time, and
   ``Settings.from_env()`` also loads ``selectors.yaml``. Anything that
   touches the filesystem happens here, never in the callers.

The module-level ``settings`` singleton exists for convenience and
backwards compatibility; new code should prefer passing a
:class:`Settings` instance explicitly, which is what makes the rest of
the codebase testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path

from dotenv import load_dotenv

from app.config.selectors import Selectors, load_selectors

# Load .env before anything reads os.environ. ``load_dotenv`` is a no-op
# if the file is missing, so this is safe in containers that inject
# variables directly.
load_dotenv()


class ConfigError(ValueError):
    """Raised when the environment cannot be turned into a valid Settings.

    Deliberately *not* a subclass of ``BotError``: this indicates a
    startup failure (the process cannot run at all), whereas
    ``BotError`` signals a recoverable runtime problem. Keeping the two
    hierarchies separate makes ``except BotError`` in the main loop safe.
    """


def _env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Read a string env var, distinguishing "unset" from "empty".

    An empty string is treated as unset: ``VAR=`` in a ``.env`` file is
    almost always a mistake, and passing it through would produce
    confusing downstream behaviour (e.g. a login form filled with "").
    """
    value = os.getenv(name, default)
    if value is None or value == "":
        if required:
            raise ConfigError(f"{name} is required but not set.")
        return ""
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Read an int env var with an optional lower bound.

    Invalid input raises :class:`ConfigError` instead of silently
    falling back to the default: a typo in ``CHECK_INTERVAL=5o`` should
    be loud, not quietly ignored.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc

    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var accepting the usual truthy spellings.

    Anything other than ``1/true/yes/on`` (case-insensitive) is false,
    so ``HEADLESS=maybe`` will quietly mean "not headless". That is
    intentional: boolean env vars are frequently written by hand, and
    rejecting typos here would be more annoying than useful.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of every knob the bot exposes.

    Fields are grouped by concern: URLs, credentials, timing,
    Playwright options, filesystem paths, logging and selectors.

    Instances should be created via :meth:`from_env` (production) or by
    calling :meth:`replace` on an existing instance (tests). Direct
    construction is supported but bypasses environment parsing, so it is
    only useful when you want full control.
    """

    # --- Portal URLs ----------------------------------------------------
    login_url: str = "https://bdvenlinea.banvenez.com/"
    accounts_url: str = "https://bdvenlinea.banvenez.com/main/posicionconsolidada"

    # --- Credentials (populated from the environment) -------------------
    username: str = ""
    password: str = ""

    # --- Loop timing ----------------------------------------------------
    check_interval: int = 300
    max_login_retries: int = 3
    headless: bool = False
    slow_mo: int = 0

    # --- Playwright timeouts (milliseconds) -----------------------------
    # ``default_timeout_ms`` applies to selectors and actions;
    # ``navigation_timeout_ms`` to page.goto. The session_* timeouts are
    # deliberately shorter so a dead session fails fast.
    default_timeout_ms: int = 15_000
    navigation_timeout_ms: int = 30_000
    session_check_timeout_ms: int = 8_000
    session_probe_timeout_ms: int = 8_000

    # --- Filesystem -----------------------------------------------------
    storage_state: Path = Path("state.json")
    results_dir: Path = Path("data")
    debug_dir: Path = Path("data/debug")
    debug_keep: int = 20

    # --- Logging --------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "text"
    log_file: Path | None = None

    # --- Selectors ------------------------------------------------------
    # ``selectors_file`` records where the overrides came from (useful
    # for logging); ``selectors`` is the resolved instance actually used
    # by the bot.
    selectors_file: Path = Path("config/selectors.yaml")
    selectors: Selectors = field(default_factory=Selectors)

    @classmethod
    def from_env(cls) -> Settings:
        """Build a :class:`Settings` from ``os.environ`` (and ``.env``).

        This is the only sanctioned entry point in production code. It
        reads every variable once, resolves paths relative to the
        current working directory, and loads ``selectors.yaml`` if
        present.
        """
        log_file_raw = _env_str("LOG_FILE")
        log_file = Path(log_file_raw) if log_file_raw else None

        selectors_file = Path(_env_str("SELECTORS_FILE", "config/selectors.yaml"))

        return cls(
            username=_env_str("BANKING_USERNAME"),
            password=_env_str("BANKING_PASSWORD"),
            check_interval=_env_int("CHECK_INTERVAL", 300, minimum=10),
            max_login_retries=_env_int("MAX_LOGIN_RETRIES", 3, minimum=1),
            headless=_env_bool("HEADLESS", False),
            slow_mo=_env_int("SLOW_MO", 0, minimum=0),
            storage_state=Path(_env_str("STORAGE_STATE", "state.json")),
            results_dir=Path(_env_str("RESULTS_DIR", "data")),
            debug_dir=Path(_env_str("DEBUG_DIR", "data/debug")),
            debug_keep=_env_int("DEBUG_KEEP", 20, minimum=0),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            log_format=_env_str("LOG_FORMAT", "text").lower(),
            log_file=log_file,
            selectors_file=selectors_file,
            selectors=load_selectors(selectors_file),
        )

    def replace(self, **overrides: object) -> Settings:
        """Return a copy with the given fields replaced.

        Convenience wrapper around :func:`dataclasses.replace`. Note
        that it does **not** call :meth:`validate`: callers that mutate
        important fields are expected to validate explicitly.
        """
        return replace(self, **overrides)

    def validate(self) -> None:
        """Check invariants and raise :class:`ConfigError` if any fail.

        All problems are collected before raising so a single run
        reports every misconfiguration at once. This is what makes
        ``_bootstrap()`` useful: the operator sees the full list in one
        go instead of fixing one issue, re-running, fixing the next…
        """
        errors: list[str] = []

        if not self.username:
            errors.append("BANKING_USERNAME is not set.")
        if not self.password:
            errors.append("BANKING_PASSWORD is not set.")

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level not in valid_levels:
            errors.append(
                f"LOG_LEVEL must be one of {sorted(valid_levels)}, got {self.log_level!r}."
            )

        if self.log_format not in {"text", "json"}:
            errors.append(f"LOG_FORMAT must be 'text' or 'json', got {self.log_format!r}.")

        if self.check_interval < 10:
            errors.append(f"CHECK_INTERVAL must be >= 10, got {self.check_interval}.")

        if self.max_login_retries < 1:
            errors.append(f"MAX_LOGIN_RETRIES must be >= 1, got {self.max_login_retries}.")

        # A navigation timeout shorter than the session probe would cause
        # the probe to always win the race and mask real login failures.
        if self.navigation_timeout_ms <= self.session_probe_timeout_ms:
            errors.append("navigation_timeout_ms must be greater than session_probe_timeout_ms.")

        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    def ensure_paths(self) -> None:
        """Create every directory the bot writes to.

        Called once at startup, before the browser launches, so that a
        failure (e.g. read-only filesystem) happens before we open
        Chromium rather than in the middle of a cycle.
        """
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def warnings(self) -> list[str]:
        """Return non-fatal configuration warnings.

        Unlike :meth:`validate`, these are cosmetic or situational: the
        bot runs fine but the operator probably wants to know. They are
        emitted as ``log.warning`` by ``_bootstrap()``.
        """
        result: list[str] = []
        if self.slow_mo > 0 and self.headless:
            result.append(f"SLOW_MO={self.slow_mo} with HEADLESS=true is pointless; set SLOW_MO=0.")
        # On POSIX without a display server, Chromium headful cannot
        # start. Warn early instead of letting Playwright fail with an
        # opaque error.
        if not self.headless and not os.environ.get("DISPLAY") and os.name == "posix":
            result.append("HEADLESS=false but DISPLAY is not set; Chromium may fail to launch.")
        return result


# Module-level singleton. Kept for backwards compatibility with code
# that imports ``config.settings`` directly. New code should prefer
# ``Settings.from_env()`` and pass the instance around, which is what
# makes the rest of the codebase testable.
settings = Settings.from_env()
