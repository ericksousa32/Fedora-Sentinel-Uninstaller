# Fedora Sentinel Uninstaller (FSU)

An intelligent, safe and surgical tool for cleaning up residual data in `$HOME`
on Fedora Linux: what RPM, Flatpak and AppImage applications that are no longer
installed leave in `~/.config`, `~/.local/share`, `~/.cache`, `~/.local/state`,
`~/.var/app`, hidden folders of `$HOME` and launcher/service folders.

> **Status:** under development (`dev` branch). Collector, safety checks,
> reversible quarantine, local AI advisor and command-line interface are in
> place.

## Requirements

- Fedora with **DNF5** (Fedora 41+); tested on Fedora 45
- Python 3.10+, no external dependencies
- Optional: `flatpak`

## Usage

```bash
git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git
cd Fedora-Sentinel-Uninstaller
pip install --user .        # installs the `fsu` command in ~/.local/bin
# or, without installing:  PYTHONPATH=src python3 -m fsu <command>
```

Run it as a **regular user**: FSU refuses to run as root, via `sudo` or via
`pkexec`.

| Command | What it does |
|---|---|
| `fsu scan [--json] [--timings] [--ai] [--no-history]` | Report of candidates (changes nothing except FSU's own history) |
| `fsu quarantine` | Shows the plan; **changes nothing** |
| `fsu quarantine --apply` | Moves the planned items to quarantine after a `[y/N]` confirmation (`--yes` for scripts) |
| `fsu quarantine --select PATH` | Includes a list-only item (always needs a Snapper snapshot) |
| `fsu quarantine --allow-without-snapshot` | Accepts medium-confidence items when no snapshot can be taken |
| `fsu list-quarantine [--json]` | Items in quarantine |
| `fsu restore ID... \| --all` | Moves items back (never overwrites) |
| `fsu purge ID... \| --all \| --older-than DAYS` | Permanent deletion, with confirmation |

Nothing is ever deleted automatically. Exit codes: `0` success, `1` error,
`2` usage error, `3` some items were blocked.

### Reading the result

| Score | Allowed action |
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
| Launcher/service whose program (path or Flatpak) is gone | 0.85 |
| Launcher/service whose bare program name is not found | 0.75 |
| FSU's own history saw the program/AppImage, and it is gone now | 0.75 |
| Similar to a removed package | 0.50 - 0.65 |
| No known match | 0.20 |
| Similar to something installed | capped at 0.30 |

## Where FSU looks

| Area | What is evaluated | Listed |
|---|---|---|
| `~/.config`, `~/.local/share`, `~/.cache` | first-level items | every band |
| `~/.var/app` | Flatpak data folders | every band |
| `~/.local/state` | first-level items | only >= 0.70 |
| `$HOME` | **hidden folders only** (never files); exact names only, no variants or similarity | only >= 0.70 |
| `~/.config/autostart`, `~/.config/systemd/user`, `~/.local/share/applications`, `~/.local/share/dbus-1/services` | `.desktop`, `.service`, `.timer` files whose program is gone | only >= 0.70 |

`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and `XDG_STATE_HOME` are
honoured when set to absolute paths (FSU's own folder follows `XDG_DATA_HOME`).

In the strict areas, items below 0.70 are never shown, only counted. In
`$HOME`, folders that usually hold **user data** are always protected, even if
their application was removed (browser/mail profiles, games, Wine prefixes,
local AI models, keys and credentials: `.mozilla`, `.thunderbird`, `.minecraft`,
`.steam`, `.wine*`, `.lmstudio`, `.ollama`, `.ssh`, `.gnupg`, `.pki`,
`.android`, `.docker`, `.kube`, ...).

Launchers and services: a program given by absolute path that no longer exists,
or a `flatpak run` of an app that is not installed, scores 0.85; a bare program
name not found in the search path scores 0.75. Entries with `Hidden=true`
(intentional overrides of system autostart entries), commands run through a
shell or interpreter, variables and removable media are never judged. A timer
is listed together with its service.

## FSU's inventory history

RPM and Flatpak record removals; an AppImage, a program in `~/.local/bin` or a
tool in `/opt` disappears without any record. So every scan records, in
`$XDG_STATE_HOME/fsu/inventory.json` (`~/.local/state/fsu/`), what exists
outside package managers: AppImage files, programs run by your launchers in
`~/.local/share/applications`, and programs outside the system directories.
On later scans, a name that **existed before and is gone now** is concrete
evidence (0.75) for the folders it left behind.

- It only works from the first scan on: it cannot recover older removals.
- An application updated to a new file name (e.g. by Gear Lever) is not "gone"
  as long as its name is still present; paths on removable media never count.
- Only paths and names are stored, locally. `--no-history` skips updating the
  file (existing evidence is still used).

List-only items also show objective **facts** that never change the score:
the last write, and whether a program, launcher or running process with the
item's name exists. Not finding one is weak evidence (apps often bundle their
tools), so it is shown, not scored.

## Sources of truth

- `rpm -qa` - installed packages
- `dnf5 history list/info --json` - removed packages
- `flatpak list --app` - installed Flatpaks
- `.desktop` files of installed applications
- AppImages in `~/AppImages`, `~/Applications`, `~/.local/bin` and user
  launchers in `~/.local/share/applications`
- programs in `$PATH`, `/opt` and pipx environments - used only as positive
  evidence: a program with the item's name means the application is installed;
  not finding one proves nothing (AppImages and Flatpaks often bundle the tools
  they use)

### Performance

A full scan takes roughly 20 seconds on a system with ~400 DNF5 transactions.
Almost all of it is `dnf5 history info --json` itself (measure with
`fsu scan --timings`); FSU's own work takes about one second.

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
fsu scan --ai                       # uses an installed model
fsu scan --ai --model gemma4:12b
```

It is **off by default**: in tests on a real system, models were right on
roughly half of the ambiguous items, so the advice is a hint, not a decision.
Traces of the same application are judged together, in a single request: the
same name in several locations (e.g. `~/.cache/x` and `~/.config/x`) and a
reverse-DNS name with its plain form (`com.example.app` and `app`).

The AI is an **advisor, never an executor**:

- items scored >= 0.70 are never sent to it;
- for list-only items (< 0.40) it gives an opinion (`residue`, `in_use`,
  `uncertain`) to help you decide, but they stay list-only;
- for medium items (0.40 - 0.70) it may only *demote* an item it believes is
  in use; it can never promote anything. Folder names are untrusted data that
  reach the prompt, so a manipulated answer can only make FSU more careful.

Privacy and robustness: only loopback addresses are accepted, system proxies
are bypassed, Ollama "cloud" models (e.g. `gpt-oss:120b-cloud`, which the
local service forwards to remote servers) are refused, only names and metadata are sent (never file contents), answers
must match a JSON schema, and after 3 consecutive failures the remaining items
are not sent. FSU never starts the Ollama service and never downloads models;
if Ollama is unavailable the scan simply continues without opinions.

## Known limitations and design decisions

These come from testing on a real Fedora 45 (KDE Plasma) system.

- **Names are only clues.** An application may store data under an old name
  (one app wrote to `hytale-rich-presence` and later to `statusify`), or under
  a name unrelated to its package. Only concrete evidence (removed package,
  missing Flatpak, missing program, missing AppImage) reaches 0.70; everything
  else stays list-only and is never moved unless you select it.
- **Access times are not used.** Programs that sweep files (indexers, search
  tools, launchers reading other apps' configs, backups) update them: on the
  test system, 142 files of unrelated applications were "accessed" within the
  same minute. FSU only uses modification times ("last write").
- **The AI is advice only.** Across three local models it was right on roughly
  half of the ambiguous items; it can demote an item, never promote one.
- **Not every write is avoided.** A scan never changes your data, but it
  updates FSU's inventory history (`~/.local/state/fsu/inventory.json`, see
  above; `--no-history` to skip), `dnf5 history` (run as your user) keeps its
  own log in `~/.local/state/dnf5.log*` (rotated by dnf5, about 5 MB at
  most), and listing folders updates their access times.
- **Other users' processes are invisible** to a regular user, so files held by
  system services cannot be detected as "in use".
- **Moves never cross file systems or Btrfs subvolumes**; such items are blocked.
- **systemd user units**: quarantining a unit leaves its enabling symlink in
  `*.wants/` dangling until you restore it or run
  `systemctl --user disable` on it.
- **Fedora Atomic** (Silverblue, Kinoite): packages removed with `rpm-ostree`
  are not in the DNF5 history, so fewer RPM residues reach high confidence.
- **One operation at a time**: commands that change the quarantine take an
  exclusive lock; a second concurrent run fails instead of racing.

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
