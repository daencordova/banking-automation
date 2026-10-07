"""Logging setup for the bot.

Two output formats are supported:

* ``text`` — human-readable, default, mirrors the classic
  ``%(asctime)s [%(levelname)s] name: message`` layout.
* ``json`` — one JSON object per line, suitable for ingestion by
  log shippers (Loki, Datadog, CloudWatch, etc.).

The console handler always writes to **stdout**. There is a known
trade-off here: business output (balance reports) also goes to stdout,
so the two can interleave. The roadmap tracks moving logs to stderr.

``setup_logging`` is idempotent: calling it twice is a no-op. This
matters because tests and the CLI both import modules that call it, and
duplicate handlers cause duplicated log lines.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
import sys

# Defaults shared between the text formatter and the JSON formatter's
# ``ts`` field, so both formats show the same timestamp layout.
DEFAULT_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Attribute set on the root logger once setup has run. Using a string
# (rather than a module-level bool) survives module reloads and is
# inspectable from a debugger.
_CONFIGURED_FLAG = "_banking_bot_configured"

# Third-party loggers that are noisy at DEBUG level. We raise their
# threshold when the app is not itself in DEBUG, to keep the signal to
# noise ratio usable.
_NOISY_LOGGERS = ("asyncio", "urllib3", "playwright")


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line.

    The base record is rendered with a stable, minimal schema::

        {"ts": "...", "level": "INFO", "logger": "app.main", "msg": "..."}

    Any extra keyword passed to a logging call (e.g.
    ``log.info("...", extra={"account": "123"})``) is merged in,
    provided it can be serialized. Values that cannot be serialized are
    stored as their ``repr`` so the log line never fails to render — a
    crash inside a formatter would be catastrophic (logging swallows
    its own exceptions, silently losing the message).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, DEFAULT_DATEFMT),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # Keys that ``LogRecord`` always defines. Anything else was
        # added via ``extra=`` and is worth surfacing.
        reserved = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
            "message",
            "asctime",
        }
        for key, value in record.__dict__.items():
            if key not in reserved and key not in payload:
                try:
                    # Probe serialization before storing: ``json.dumps``
                    # raises for non-serializable values, and we want to
                    # fall back to ``repr`` in that case, not fail the
                    # whole log line.
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


def _make_formatter(fmt: str) -> logging.Formatter:
    """Return the formatter matching ``fmt`` (``"json"`` or anything else)."""
    if fmt == "json":
        return JsonFormatter()
    return logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)


def setup_logging(
    level: str = "INFO",
    log_file: Path | str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    fmt: str = "text",
) -> None:
    """Configure the root logger. Idempotent.

    Parameters
    ----------
    level : str
        Threshold name (``"DEBUG"``, ``"INFO"``, …). Unknown names fall
        back to ``INFO`` rather than raising, so a bad ``LOG_LEVEL`` in
        ``.env`` still produces logs.
    log_file : Path | str | None
        If given, a rotating file handler is added alongside the console
        one. Parent directories are created.
    max_bytes, backup_count : int
        Rotation policy for the file handler.
    fmt : str
        ``"json"`` for structured output, anything else for text.

    Notes:
    -----
    Idempotency is enforced via an attribute on the root logger rather
    than a module-level flag: this survives ``importlib.reload`` and
    makes the state visible in ``logging.getLogger().__dict__``.
    """
    root = logging.getLogger()

    if getattr(root, _CONFIGURED_FLAG, False):
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    formatter = _make_formatter(fmt)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Only silence noisy third parties when the app itself is not in
    # DEBUG: a developer who set LOG_LEVEL=DEBUG probably wants to see
    # Playwright's chatter too.
    if numeric_level > logging.DEBUG:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    setattr(root, _CONFIGURED_FLAG, True)


def shutdown_logging() -> None:
    """Flush and close every logging handler.

    Called from the signal handler before ``os._exit`` so buffered log
    records are not lost when the process is forced down.
    """
    logging.shutdown()
