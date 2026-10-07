from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class BotError(Exception):
    pass


class LoginError(BotError):
    def __init__(
        self,
        message: str,
        *,
        step: str | None = None,
        url: str | None = None,
        debug_dir: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.url = url
        self.debug_dir = debug_dir


class ScrapingError(BotError):
    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        account: str | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.account = account
