# Fedora Sentinel Uninstaller (FSU)

An intelligent, safe and surgical tool for cleaning up residual data in `$HOME`
on Fedora Linux: folders left in `~/.config`, `~/.local/share`, `~/.cache` and
`~/.var/app` by RPM, Flatpak and AppImage applications that are no longer
installed.

> **Status:** under development. The current stage (collector) is
> **read-only**: nothing is moved or deleted.

## Requirements

- Fedora with **DNF5** (Fedora 41+); tested on Fedora 45
- Python 3.10+, no external dependencies
- Optional: `flatpak`

## Current usage (stage 1 - read-only scan)

```bash
git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git
cd Fedora-Sentinel-Uninstaller
PYTHONPATH=src python3 -m fsu.collector             # table
PYTHONPATH=src python3 -m fsu.collector --json      # JSON
PYTHONPATH=src python3 -m fsu.collector --timings   # time spent per phase
```

Run it as a **regular user** (never with `sudo`).

### Reading the result

| Score | Allowed action (future stages) |
|---|---|
| >= 0.70 | Direct quarantine |
| 0.40 - 0.70 | Quarantine only with a preventive snapshot |
| < 0.40 | Listed only |

| Evidence | Score |
|---|---|
| `~/.var/app/<id>` of a Flatpak that is not installed | 0.95 |
| Exact name of a removed RPM package | 0.85 |
| Exact match through a name variant (`<app>rc`, `<app>-...`) | 0.80 |
| Launcher pointing to an AppImage that no longer exists | 0.75 |
| Similar to a removed package | 0.50 - 0.65 |
| No known match | 0.20 |
| Similar to something installed | capped at 0.30 |

## Sources of truth

- `rpm -qa` - installed packages
- `dnf5 history list/info --json` - removed packages
- `flatpak list --app` - installed Flatpaks
- `.desktop` files of installed applications
- AppImages in `~/AppImages`, `~/Applications`, `~/.local/bin` and user
  launchers in `~/.local/share/applications`

## Safety principles

- FSU runs as a regular user; it never operates on files as root.
- Symlinks are never followed; mount points are not crossed.
- If a source of truth (rpm, dnf5, flatpak) fails, the category that depends
  on it is skipped - things never all become "orphaned".
- A protection list covers critical session items (dconf, keyrings,
  autostart, Plasma/GNOME/Qt, Flatpak, containers...) and FSU's own folder.
- Folders recreated by other software are not candidates. Example: Plasma
  Browser Integration writes stubs for Flatpak browsers into `~/.var/app` at
  every login (it can be disabled in *System Settings > Background Services >
  Plasma Browser Integration Flatpak Integration*).

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not fedora" && ruff check . && ruff format --check . && mypy
```

Tests marked `fedora` run against real `rpm`/`dnf5` (CI job in a
`fedora:latest` container).

## License

GPL-3.0-or-later.
