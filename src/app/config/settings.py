from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path

from dotenv import load_dotenv

from app.config.selectors import Selectors, load_selectors

load_dotenv()


class ConfigError(ValueError):
    pass


def _env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        if required:
            raise ConfigError(f"{name} is required but not set.")
        return ""
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
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
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    login_url: str = "https://bdvenlinea.banvenez.com/"
    accounts_url: str = "https://bdvenlinea.banvenez.com/main/posicionconsolidada"

    username: str = ""
    password: str = ""

    check_interval: int = 300
    max_login_retries: int = 3
    headless: bool = False
    slow_mo: int = 0

    default_timeout_ms: int = 15_000
    navigation_timeout_ms: int = 30_000
    session_check_timeout_ms: int = 8_000
    session_probe_timeout_ms: int = 8_000

    storage_state: Path = Path("state.json")
    results_dir: Path = Path("data")
    debug_dir: Path = Path("data/debug")
    debug_keep: int = 20

    log_level: str = "INFO"
    log_format: str = "text"
    log_file: Path | None = None

    selectors_file: Path = Path("config/selectors.yaml")

    selectors: Selectors = field(default_factory=Selectors)

    @classmethod
    def from_env(cls) -> Settings:
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
        return replace(self, **overrides)

    def validate(self) -> None:
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

        if self.navigation_timeout_ms <= self.session_probe_timeout_ms:
            errors.append("navigation_timeout_ms must be greater than session_probe_timeout_ms.")

        if errors:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    def ensure_paths(self) -> None:
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def warnings(self) -> list[str]:
        result: list[str] = []
        if self.slow_mo > 0 and self.headless:
            result.append(f"SLOW_MO={self.slow_mo} with HEADLESS=true is pointless; set SLOW_MO=0.")
        if not self.headless and not os.environ.get("DISPLAY") and os.name == "posix":
            result.append("HEADLESS=false but DISPLAY is not set; Chromium may fail to launch.")
        return result


settings = Settings.from_env()
