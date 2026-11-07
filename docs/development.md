# Development

## Layout

```
src/fsu/
├── cli.py         command-line interface (fsu scan / quarantine / list-quarantine / restore / purge)
├── collector.py   the scan: inventories → first-level items → protection → scoring → report
├── inventory.py   sources of truth: rpm, dnf5 history, flatpak, .desktop, AppImages, programs, commands
├── history.py     FSU's own inventory history ("seen then gone")
├── heuristics.py  name normalization, variants, NameIndex, scores and bands
├── protected.py   protection lists and scan-area membership
├── paths.py       scan areas, XDG directories, FSU's own folders
├── safeguard.py   read-only checks before a move; Snapper integration
├── quarantine.py  quarantine store (move/restore/purge/recover/lock) and batch execution
├── ai_engine.py   optional local AI advisor (Ollama), neutral input, advice only
├── system.py      the only place that runs external commands
└── models.py      shared immutable data types
```

Data flows one way: `cli` → `collector` (→ `inventory`, `history`,
`heuristics`, `protected`) → optional `ai_engine` → `quarantine`
(→ `safeguard`).

## Setting up

Set up a development environment (run once; creates `.venv` inside the
repository):

```bash
cd ~/Fedora-Sentinel-Uninstaller
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run all checks (the same ones CI runs, except the Fedora-only tests):

```bash
cd ~/Fedora-Sentinel-Uninstaller
.venv/bin/pytest -m "not fedora" && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

- Python 3.10+; **no runtime dependencies** (standard library only).
- `ruff` for lint and formatting, `mypy --strict` for types.
- All code, comments, messages and documentation are in English.

## Tests

| Kind | Where | Runs |
|---|---|---|
| Unit tests | `tests/test_*.py` | everywhere; external commands are replaced by `FakeRunner`, the home folder is a temporary directory |
| Real `/proc` tests | `test_safeguard.py`, `test_quarantine.py` | start real helper processes to check "in use" detection |
| Fake Ollama | `tests/helpers.py` (`FakeOllama`) | an in-process HTTP server; no real model needed |
| Fedora integration | `test_fedora_integration.py` (`@pytest.mark.fedora`) | CI, inside a `fedora:latest` container with real `rpm`/`dnf5` |
| CLI smoke test | `.github/workflows/ci.yml` | CI, as a regular user: scan, history, apply, list, restore, root refusal |

Tests are isolated from the machine they run on: `tests/conftest.py` hides
the host's `$PATH`, `/opt`, pipx, running processes and `XDG_*` variables.
Some tests behave differently as root (permission tests are skipped); run the
suite as a normal user too.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push to `dev` and
`main` and on pull requests to `main`:

1. **Unit** — Python 3.10 and 3.13: lint, types, tests.
2. **Integration (real Fedora)** — creates a real DNF5 removal (`cowsay`),
   runs the Fedora tests, then the CLI smoke test as a regular user.

## Workflow

- Work happens on `dev`; `main` receives reviewed pull requests after the
  changes were tested on a real system.
- Each change: plan → approval → implementation with tests → CI → test on a
  real Fedora system.

## Rules for changing FSU

These keep FSU safe and replicable. Please follow them in contributions.

1. **Safety first.** A false "protected" is acceptable; a false "residue" is a
   bug. New evidence that can reach 0.70 must be concrete and verifiable.
2. **No rules for one specific app or system.** Changes must describe how
   software works in general (a naming convention, an ownership, a file
   format), never a fix for one item seen on one machine.
3. **Absence is not evidence.** Not finding something may be shown to the
   user, but never raises a score and is never sent to the AI.
4. **The AI never decides.** It may only lower a band. Its input stays
   neutral: no scores, sizes, dates, negative findings or software names in
   the prompt (`test_ai_engine.py` guards this).
5. **One place for external commands.** Use `fsu.system.run_command`: no
   shell, a timeout, a fixed locale, typed errors.
6. **Read-only means read-only.** Scans write only FSU's own history; every
   other change goes through the quarantine, with checks, confirmation and a
   manifest.
7. **Document what you learn.** Behaviour discovered on real systems goes into
   [Known limitations](limitations.md), with the reason.
