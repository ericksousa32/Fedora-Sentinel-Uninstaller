# Fedora Sentinel Uninstaller (FSU)

An intelligent, safe and surgical tool for cleaning up residual data in `$HOME`
on Fedora Linux: folders left in `~/.config`, `~/.local/share`, `~/.cache` and
`~/.var/app` by RPM, Flatpak and AppImage applications that are no longer
installed.

> **Status:** under development. Stage 1 (read-only collector), stage 2
> (safety checks and reversible quarantine) and stage 3 (local AI advisor)
> are done as Python APIs; the command-line interface comes in stage 4.

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
| Folder owned by a removed package (e.g. `NuGet` -> `dotnet*`) | 0.80 |
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

### Performance

A full scan takes roughly 20 seconds on a system with ~400 DNF5 transactions.
Almost all of it is `dnf5 history info --json` itself (measure with
`--timings`); FSU's own work takes about one second.

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

## Quarantine (stage 2)

Nothing is ever deleted directly. Items are moved with a single atomic
`rename()` into `~/.local/share/fsu/quarantine/<id>/payload`, next to a
`manifest.json` describing where they came from; every operation is logged in
`~/.local/share/fsu/log.jsonl`. Restoring never overwrites an existing path,
and a tampered manifest cannot restore anything outside the scan roots.

Before any move (`fsu.safeguard`):

- FSU refuses to run as root, through `sudo` or through `pkexec`;
- the item is re-validated (still there, still in scope, no symlinked parent);
- every file inside must belong to the user and be readable;
- no mount point or Btrfs subvolume inside the item;
- item and quarantine must share a file system (no copy-based moves);
- no process of the user may have files open or its working directory inside
  the item, and the owning application must not be running. Linux hides
  other users' processes, so files held by system services cannot be seen.

Score policy:

| Score | Rule |
|---|---|
| >= 0.70 | Quarantine directly |
| 0.40 - 0.70 | Requires a Snapper snapshot, or explicit confirmation if Snapper is unavailable |
| < 0.40 | Only if explicitly selected, and always with a Snapper snapshot |

### Snapper

FSU never configures Snapper. It uses a config whose subvolume is the mount
point of `$HOME` and that the user may access. To enable it (once, as admin):

```bash
sudo snapper -c home create-config /home
sudo snapper -c home set-config ALLOW_USERS=$USER SYNC_ACL=yes
# optional: no hourly timeline snapshots, only the ones FSU asks for
sudo snapper -c home set-config TIMELINE_CREATE=no
```

One snapshot is created per batch, only when a batch item needs it.

## Local AI advisor (stage 3)

An optional second opinion from a model running in [Ollama](https://ollama.com)
on the same machine:

```bash
PYTHONPATH=src python3 -m fsu.ai_engine            # auto-selects an installed model
PYTHONPATH=src python3 -m fsu.ai_engine --model qwen2.5:7b
```

The AI is an **advisor, never an executor**:

- items scored >= 0.70 are never sent to it;
- for list-only items (< 0.40) it gives an opinion (`residue`, `in_use`,
  `uncertain`) to help you decide, but they stay list-only;
- for medium items (0.40 - 0.70) it may only *demote* an item it believes is
  in use; it can never promote anything. Folder names are untrusted data that
  reach the prompt, so a manipulated answer can only make FSU more careful.

Privacy and robustness: only loopback addresses are accepted, system proxies
are bypassed, only names and metadata are sent (never file contents), answers
must match a JSON schema, and after 3 consecutive failures the remaining items
are not sent. FSU never starts the Ollama service and never downloads models;
if Ollama is unavailable the scan simply continues without opinions.

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
