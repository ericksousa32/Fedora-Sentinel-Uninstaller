# Getting started

This guide takes you from zero to your first clean-up. It takes about ten
minutes, most of which is reading what FSU found.

## 1. Requirements

- **Fedora 41 or newer** (FSU reads the DNF5 history; tested on Fedora 45).
- **Python 3.10+** — already part of Fedora. No other packages are needed.
- Optional: **Flatpak** (to evaluate `~/.var/app`), **Snapper** (for
  snapshots, see [Safety model](safety.md#snapshots)), **Ollama** (for the
  optional [AI advisor](ai-advisor.md)).

## 2. Install

```bash
git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git
cd Fedora-Sentinel-Uninstaller
pip install --user .
```

This installs the `fsu` command in `~/.local/bin`. If you prefer not to
install anything, run it from the cloned folder instead:

```bash
alias fsu='PYTHONPATH=src python3 -m fsu'
```

> **Always run FSU as your normal user.** It refuses to run as root, through
> `sudo` or through `pkexec` — your home folder should never be cleaned by a
> privileged process.

## 3. First scan

```bash
fsu scan
```

The scan takes around 20 seconds on a typical system (almost all of it is
`dnf5` reading its history). It **changes nothing** in your files. You will
see a list like this:

```
0.85  quarantine                     668 B  ~/.config/bleachbit
      └─ removed package: bleachbit
0.80  quarantine                 372.0 MiB  ~/.nuget
      └─ folder owned by removed package(s): dotnet-host, dotnet-runtime-8.0 (+5 more)
0.57  quarantine_with_snapshot    97.7 KiB  ~/.config/dosbox
      └─ weak name similarity (46%) to removed package 'dosbox-staging'
0.20  list_only                    2.2 GiB  ~/.cache/nuvio
      └─ no known match; last write 1 day(s) ago; no program, launcher or running process with exactly this name ('nuvio')
```

Each line is a *candidate*: something that may be left over. The number is
FSU's **confidence** that it belongs to software that is gone, and the line
below it is the **evidence**. In short:

- **0.70 or more** — concrete evidence; proposed for clean-up.
- **0.40 to 0.69** — a clue, not proof; needs a safety snapshot.
- **below 0.40** — just listed for your information; never moved unless you
  choose it.

[Reading the results](reading-the-results.md) explains every kind of line in
detail.

## 4. See the plan

```bash
fsu quarantine
```

This shows exactly what *would* be moved and what is blocked, and why. It
still **changes nothing**:

```
Plan:
  would-quarantine  ~/.config/bleachbit
  would-quarantine  ~/.nuget
  blocked           ~/.config/dosbox
      └─ snapshot-required: medium-confidence item: needs a Snapper snapshot or explicit confirmation
19 list-only item(s) not considered (use --select PATH to choose).
Dry run: nothing was changed. Add --apply to quarantine the items above.
```

## 5. Clean up

```bash
fsu quarantine --apply
```

FSU shows the plan again and asks before doing anything:

```
Move 14 item(s) (457.3 MiB) to quarantine? [y/N]
```

Answer `y` to proceed. Each item is checked once more right before it is
moved (is it still there? is a program using it right now?). Items move to
`~/.local/share/fsu/quarantine/` instantly — nothing is copied or deleted.

## 6. Live with it for a few days

Use your computer normally. If an application complains that its settings
are gone, put them back:

```bash
fsu list-quarantine          # find the item's ID
fsu restore 20261107T175332-473f1c
# or put everything back:
fsu restore --all
```

## 7. Delete for good (optional)

When you are confident nothing is missing — a week is a good rule of thumb:

```bash
fsu purge --older-than 7
```

This is the **only irreversible step**, and FSU asks for confirmation:

```
PERMANENTLY delete 14 item(s) (457.3 MiB)? This cannot be undone. [y/N]
```

## What next?

- Curious about an item marked `list_only`? See
  [How to decide about list-only items](reading-the-results.md#deciding-about-list-only-items).
- Want a second opinion on ambiguous items? Try the
  [AI advisor](ai-advisor.md) (`fsu scan --ai`).
- Want medium-confidence items handled with a safety net? Set up
  [Snapper snapshots](safety.md#snapshots).
