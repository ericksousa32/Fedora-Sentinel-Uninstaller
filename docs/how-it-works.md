# How it works

A scan answers one question for every item it looks at: **does this belong to
software that is no longer installed — and what is the evidence?**

```
 inventories ──► scan $HOME ──► protect / skip installed ──► score ──► report
 (what exists,    (first level   (never touched)              (evidence
  what was        only, no                                     → 0..1)
  removed)        symlinks)
```

## 1. Where FSU looks

FSU looks only at the **first level** of these places — it never walks into
arbitrary folders and never follows symbolic links.

| Area | What is evaluated | Shown |
|---|---|---|
| `~/.config`, `~/.local/share`, `~/.cache` | every item | all scores |
| `~/.var/app` | Flatpak data folders | all scores |
| `~/.local/state` | every item | only ≥ 0.70 |
| `$HOME` | **hidden folders only** (never files) | only ≥ 0.70 |
| `~/.config/autostart`, `~/.config/systemd/user`, `~/.local/share/applications`, `~/.local/share/dbus-1/services` | `.desktop`, `.service`, `.timer` files | only ≥ 0.70 |

The last three are **strict areas**: they hold things that matter more (the
root of your home, session state, programs that start automatically), so only
items with concrete evidence are ever shown there.

If you set `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` or
`XDG_STATE_HOME`, FSU uses those locations instead of the defaults.

## 2. Sources of truth

| Source | Tells FSU | How |
|---|---|---|
| RPM database | what is installed now | `rpm -qa` |
| DNF5 history | what was removed, and when | `dnf5 history list/info --json` |
| Flatpak | installed Flatpak apps (user and system) | `flatpak list --app` |
| `.desktop` files | names of installed apps (including `StartupWMClass`) | system and Flatpak application folders |
| AppImages | AppImages present in `~/AppImages`, `~/Applications`, `~/.local/bin`; launchers pointing to them | file system |
| Programs | programs in `$PATH`, `/opt` and pipx environments | file system |
| FSU's inventory history | what *used to* exist outside package managers | `~/.local/state/fsu/inventory.json` |

All commands run without a shell, with a timeout and with an English locale
(so localized systems give predictable output). If a source fails, FSU skips
the areas that depend on it and tells you — it never assumes "everything is
orphaned".

## 3. Skipping what is safe to skip

Before scoring, FSU removes from consideration:

- **Protected items** — your desktop session, keyrings, identity and
  credentials, folders that typically hold your own data, and FSU's own
  folder. See [Safety model](safety.md#protected-items).
- **Installed items** — anything whose name matches installed software:
  a package, a Flatpak, a `.desktop` file, an AppImage, a program, or a known
  owner (for example `~/.mozilla` belongs to Firefox).
- **Recreated items** — folders another program recreates at every login
  (Plasma Browser Integration stubs in `~/.var/app`).

### How names are matched

Folder names rarely equal package names exactly, so FSU compares
*normalized* names (lower case, letters and digits only) and a few
well-defined *variants*:

| Item | Variant | Why |
|---|---|---|
| `kolourpaintrc`, `kfontinstuirc`, `krunnerstaterc` | `kolourpaint`, `kfontinst`, `krunner` | KDE configuration naming (`<app>rc`, `<app>staterc`, …) |
| `sealert.conf` | `sealert` | file extensions |
| `kunifiedpush-org.kde.neochat` | `kunifiedpush` | the part before `-`, `_` or `.` (at least 4 letters) |

Some folders are not named after their package at all. A small table of
**owners** covers well-known cases, e.g. `NuGet`/`.nuget`/`.dotnet` → `dotnet*`,
`mozilla` → Firefox/Thunderbird, `wget` → `wget`/`wget2`.

In `$HOME` itself only **exact** names count — no variants, no similarity.

## 4. Scoring

| Evidence | Score |
|---|---|
| Flatpak data folder of an app that is not installed | 0.95 |
| Exact name of a removed package | 0.85 |
| Launcher/service runs a path or Flatpak that is gone | 0.85 |
| Exact match through a name variant | 0.80 |
| Folder owned by removed package(s) | 0.80 |
| Launcher points to an AppImage that no longer exists | 0.75 |
| FSU's history saw the program or AppImage, and it is gone now | 0.75 |
| Launcher/service runs a program name found nowhere | 0.75 |
| Name similar to a removed package | 0.50 – 0.65 |
| No known match | 0.20 |
| Name similar to something installed | capped at 0.30 |

The scores are fixed rules, not statistics: each one corresponds to a kind of
evidence you can verify yourself.

## 5. Launchers and services

For files in the launcher areas, FSU reads the command they would run and
checks whether the program still exists:

- an absolute path that no longer exists → 0.85;
- `flatpak run <id>` for an app that is not installed → 0.85;
- a bare program name found nowhere in the search path → 0.75.

Never judged (and never shown): entries with `Hidden=true` (these are
intentional overrides that *disable* a system autostart entry), commands run
through a shell or interpreter (`sh -c …`, `python3 script.py`), commands
using variables, and programs on removable media. A `.timer` is listed
together with the `.service` it starts.

## FSU's inventory history

Package managers remember removals; an AppImage or a downloaded program does
not. So every scan writes down what currently exists **outside** package
managers:

- AppImage files;
- programs run by your launchers in `~/.local/share/applications`;
- programs outside the system directories (`~/.local/bin`, `/usr/local/bin`,
  `/opt`, pipx).

On later scans, a name that **existed before and is gone now** becomes
concrete evidence (0.75) for the folders it left behind:

```
0.75  quarantine   ~/.config/someapp
      └─ appimage ~/AppImages/someapp-1.2.AppImage was present on 2026-11-05 and is gone
```

Good to know:

- It works from your first scan onwards; it cannot see removals that happened
  before FSU was installed.
- An app updated to a new file name (as Gear Lever does) is not "gone" while
  its name is still present. Paths on removable media never count.
- Only paths and names are stored, locally. `--no-history` skips updating the
  file; existing evidence is still used.

## 6. Facts for list-only items

Items without evidence get a line of facts — last write, and whether a
program, launcher or running process with exactly that name exists. Facts
never change the score; they are there to help you decide (see
[Reading the results](reading-the-results.md#facts-list-only-items)).
