"""Banking automation bot.

Top-level package. Importing it should have no side effects: no
environment reading, no logging configuration, no filesystem access.
Those concerns live in :mod:`app.config` and are triggered explicitly
by :mod:`app.main`.

Submodules
----------
main
    CLI, loop and process-level concerns.
browser
    Playwright lifecycle.
login
    Login flow and session probing.
accounts
    Balance scraping.
config
    Settings, selectors and logging setup.
errors
    Exception hierarchy.
"""

__version__ = "0.1.0"
