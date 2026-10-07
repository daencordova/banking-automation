# Banking Bot

A small, resilient Python bot that logs into a Venezuelan online banking portal (BDV en Línea), reads the balances of every account on the dashboard, and persists each cycle's results as a JSON snapshot.

It is designed to run unattended: it restores its session from disk, retries gracefully, backs off on repeated failures, and shuts down cleanly on SIGINT / SIGTERM.

> **Disclaimer** — This project is a personal automation tool. Use it only with accounts you own and only if doing so does not violate your bank's terms of service. The authors are not responsible for any consequences of using this software.

---

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Running as a long-lived process](#running-as-a-long-lived-process)
  - [Running a single cycle](#running-a-single-cycle)
  - [Passing arguments through](#passing-arguments-through)
- [Project layout](#project-layout)
- [How it works](#how-it-works)
- [Output format](#output-format)
- [Development](#development)
- [Conventions](#conventions)
- [Troubleshooting](#troubleshooting)
- [Security](#security)

---

## Features

- 🔐 **Session reuse** — Persists cookies to `state.json` so the bot does not log in on every cycle.
- 🔁 **Retry with back-off** — Up to `_MAX_NETWORK_FAILURES` consecutive failures before giving up, with jittered exponential back-off (`_BACKOFF_BASE` → `_BACKOFF_CAP`) between cycles.
- 🧩 **Configurable selectors** — All DOM selectors live in a single frozen dataclass (`app.config.selectors.Selectors`), optionally overridden from `config/selectors.yaml`, so a UI change is a one-file edit.
- 🖥️ **Headful or headless** — Toggle via `HEADLESS=true|false`.
- 📦 **JSON snapshots** — Every successful cycle writes `data/balances_YYYYMMDD_HHMMSS.json`.
- 🛑 **Graceful shutdown** — SIGINT / SIGTERM stop the loop after the current cycle finishes; a second signal forces immediate exit.
- 🧪 **Typed and validated config** — A frozen `Settings` dataclass is built from the environment and validated before the loop starts.
- 🩺 **Debug artifacts** — On login failure, a screenshot, HTML dump and URL are written to `data/debug/` and pruned automatically.
- 📝 Structured logging — `LOG_FORMAT=text|json` for human or machine consumption, with optional rotating log file.

---

## Requirements

- Python 3.11 or 3.12 (Playwright does not ship wheels for 3.13+ yet).
- A Unix-like OS (Linux, macOS). Windows is not officially supported because of SIGTERM handling in `app/main.py`.
- Chromium binaries — installed automatically by `run.sh` via `playwright install chromium`.
- Valid credentials for the target portal (see [Configuration](#configuration)).

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/daencordova/banking-automation
cd banking-automation

# 2. Create .env (see Configuration below)
cp .env.example .env
$EDITOR .env

# 3. Run
./run.sh
```

`run.sh` will:
1. Validate the working directory.
2. Check the Python interpreter version.
3. Create `.venv` if missing.
4. Install dependencies (editable) and Playwright's Chromium.
5. Source the virtual environment.
6. Launch the bot with `python -m app.main`.

### run.sh flags

| Flag | Description |
| :--- | :--- |
| `-h`, `--help` | Show help and exit. |
| `-r`, `--reinstall` | Delete `.venv` and reinstall everything. |
| `-n`, `--no-install` | Skip dependency installation (use the venv as-is). |
| `--` | Pass all remaining arguments through to the bot. |

### run.sh environment variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PYTHON_BIN` | `python3.12` | Python interpreter used to create the venv. |

---

## Configuration

All configuration is read from environment variables, optionally loaded from a `.env` file at the project root.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `BANKING_USERNAME` | *(required)* | Portal username. |
| `BANKING_PASSWORD` | *(required)* | Portal password. |
| `CHECK_INTERVAL` | `300` | Seconds between cycles (min 10). |
| `MAX_LOGIN_RETRIES` | `3` | Consecutive failures before exiting (min 1). |
| `HEADLESS` | `false` | Run Chromium headless (`true`/`false`). |
| `SLOW_MO` | `500` | Playwright `slow_mo` in ms (debug only). |
| `STORAGE_STATE` | `state.json` | Path to the persisted cookies file. |
| `RESULTS_DIR` | `data` | Directory for JSON snapshots. |
| `DEBUG_DIR` | `data/debug` | Directory for login-failure artifacts. |
| `DEBUG_KEEP` | `20` | Number of debug artifact groups to keep. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `LOG_FORMAT` | `text` | `text` or `json`. |
| `LOG_FILE` | *(unset)* | Optional rotating log file path. |
| `SELECTORS_FILE` | `config/selectors.yaml` | Optional selector overrides. |

### Example `.env`

```dotenv
BANKING_USERNAME=myuser
BANKING_PASSWORD=supersecret
CHECK_INTERVAL=600
HEADLESS=true
SLOW_MO=500
LOG_LEVEL=INFO
LOG_FILE=logs/banking-automation.log
STORAGE_STATE=state.json
```

---

## Usage

### Running as a long-lived process

```bash
./run.sh
```

The bot loops until interrupted. Press `Ctrl+C` or send `SIGTERM`; the current cycle will finish and the process will exit cleanly. A second signal forces immediate exit.

### Running a single cycle

```bash
./run.sh --once
```

Runs exactly one cycle and exits. Useful for cron, CI, or one-off checks.

### Passing arguments through

```bash
./run.sh -- --help
```

Anything after `--` is forwarded verbatim to `python -m app.main`.

---

## Project layout

```text
banking-automation/
├── run.sh                     # Bootstrap + entry point
├── Makefile                   # Developer shortcuts (gencode, lint, test…)
├── pyproject.toml             # Project metadata and dependencies
├── config/
│   └── selectors.yaml         # Optional selector overrides
├── .env                       # Local secrets (git-ignored)
├── state.json                 # Persisted cookies (git-ignored)
├── data/                      # JSON snapshots per cycle
│   └── debug/                 # Login-failure artifacts
├── logs/                      # Optional rotating logs
└── src/
    ├── app/
    │   ├── __init__.py        # Package metadata
    │   ├── main.py            # CLI, loop, signal handling, persistence
    │   ├── browser.py         # BrowserSession context manager
    │   ├── login.py           # Login flow + session probe
    │   ├── accounts.py        # Scraping + balance formatting
    │   ├── keepalive.py       # Session keepalive during sleep
    │   ├── errors.py          # BotError, LoginError, ScrapingError
    │   └── config/
    │       ├── __init__.py    # Re-exports
    │       ├── settings.py    # Settings dataclass, env parsing
    │       ├── selectors.py   # Selectors dataclass + YAML loader
    │       └── logging.py     # setup_logging (idempotent)
    ├── scripts/
    │   └── parse_code.py      # Code generation helper (see Makefile)
    └── tests/
        ├── test_selectors.py
        └── ...
```

---

## How it works

```text
        ┌──────────────────────────────────────────────┐
        │                  main._run_loop              │
        └──────────────────────┬───────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  BrowserSession     │  (context manager)
                    │  · launch Chromium  │
                    │  · restore state    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  ensure_logged_in   │
                    │  · probe session    │
                    │  · fallback: login  │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  fetch_balances     │
                    │  · for each row:    │
                    │      open dialog    │
                    │      read values    │
                    │      close dialog   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  print + persist    │
                    │  · stdout           │
                    │  · data/*.json      │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  keepalive sleep    │
                    │  · reload every Ns  │
                    │  · jitter + backoff │
                    └─────────────────────┘
```

1. **Bootstrap** — `_bootstrap()` configures logging from `Settings`, validates credentials and creates required directories.
2. **Cycle** — For each iteration, a `BrowserSession` is opened. If `state.json` exists, cookies are restored; otherwise a fresh context is created.
3. **Session probe** — `ensure_logged_in()` navigates to the accounts URL. If the accounts table appears within `session_probe_timeout_ms`, the session is reused. Otherwise a full login runs.
4. **Login** — `login()` fills the username, submits, fills the password, submits, waits for `/main/**`, then re-verifies the accounts table. Only on success is the session marked dirty so cookies get persisted on exit.
5. **Scrape** — `fetch_balances()` iterates the accounts table. For each row it opens the balance dialog, reads the deferred and available amounts, and closes the dialog. A failing row is recorded as `"N/A"` instead of aborting the cycle.
6. **Persist** — `print_balances()` writes a human-readable block to `stdout`, and `_save_results()` writes a timestamped JSON file under `data/`.
7. **Keepalive sleep** — Between cycles, `sleep_with_keepalive()` reloads the accounts URL periodically (every 60–120 s) to keep the session warm. If the session dies, the next cycle skips the probe and logs in directly. Network errors during keepalive count toward the back-off counter.

---

## Output format

### `stdout` (per cycle)

```text
============================================================
 Banking Account Balances
============================================================
 Account    : CUENTA AHORRO
 Number     : 0102***6690
 Deferred   : 0,00 Bs.
 Available  : 1.234,56 Bs.

 Account    : CUENTA CORRIENTE
 Number     : 0102***1234
 Deferred   : 0,00 Bs.
 Available  : 789,00 Bs.
============================================================
```

### `data/balances_YYYYMMDD_HHMMSS.json`

```json
{
  "schema_version": 1,
  "timestamp": "20250115_143012",
  "accounts": [
    {
      "account_type": "CUENTA AHORRO",
      "account_number": "0102***6690",
      "deferred_balance": "0,00 Bs.",
      "available_balance": "1.234,56 Bs."
    }
  ]
}
```

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -q

# With coverage
pytest --cov=app --cov-report=term-missing

# Lint and format
ruff check src tests
ruff format --check src tests

# Type-check
mypy src
```

### Makefile shortcuts

| Target | Description |
| :--- | :--- |
| `make gencode` | Regenerate code from `src/scripts/parse_code.py`. |
| `make run` | Run `./run.sh`. |
| `make reinstall` | Run `./run.sh --reinstall`. |
| `make clean` | Remove `.venv`, caches and `__pycache__`. |
| `make lint` | Run `ruff check` and `ruff format --check`. |
| `make test` | Run `pytest -v`. |

---

## Conventions

- Python 3.11+ syntax. `from __future__ import annotations` is present but optional.
- Configuration is never read from `os.environ` outside `app/config/settings.py`.
- Selectors live only in `Selectors`; never hard-code a CSS string in a module. Override them via `config/selectors.yaml`.
- Prefer `dataclass(frozen=True)` for value objects.
- All public functions and classes should have docstrings.

### Adding a new selector

1. Add the field to `Selectors` in `app/config/selectors.py` with a sensible default.
2. (Optional) Add an override key to `config/selectors.yaml`.
3. Use `settings.selectors.<field>` from the caller.
4. Add an assertion in `tests/test_selectors.py` that the selector matches a fixture HTML snippet.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `BANKING_USERNAME` is not set. | Missing `.env` or empty var. | Create `.env` next to `pyproject.toml`. |
| Bank rejected login: ... | Wrong credentials or locked account. | Log into the portal manually to confirm. |
| Timed out during login flow at step: ... | Slow network, or selector drift. | Increase `SLOW_MO`, set `HEADLESS=false`, inspect `data/debug/`. |
| Accounts table not visible after login. | Selector drift (UI changed). | Update `accounts_table` in `config/selectors.yaml`. |
| Balance shows `N/A` | Dialog did not open or had no rows. | Run headful, check `balance_rows` selector. |
| `python: command not found` inside `run.sh` | Python < 3.11 or wrong `PYTHON_BIN`. | `PYTHON_BIN=python3.11 ./run.sh`. |
| Chromium fails to launch (no DISPLAY) | `HEADLESS=false` on a headless box. | Set `HEADLESS=true`. |
| Logs interleaved with balances | Both go to stdout. | See Roadmap — planned split to stderr. |

---

## Security

- `state.json` contains live session cookies. Treat it with the same care as your password: keep it out of version control, and consider `chmod 600 state.json` (the bot already sets `0o600` on POSIX when it writes it).
- `.env` contains your banking credentials. Never commit it.
- `data/debug/` may contain screenshots of the portal; review before sharing.
- The bot writes nothing outside the project directory by default. If you relocate `STORAGE_STATE`, `RESULTS_DIR`, `DEBUG_DIR` or `LOG_FILE`, make sure the destination is equally protected.

