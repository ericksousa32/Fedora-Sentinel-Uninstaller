# Getting started

This guide takes you from zero to your first clean-up. It takes about ten
minutes, most of which is reading what FSU found.

## 1. Requirements

- **Fedora 41 or newer** (FSU reads the DNF5 history; tested on Fedora 45).
- **Python 3.10+** — already part of Fedora. No other packages are needed.
- Optional: **Flatpak** (to evaluate `~/.var/app`), **Snapper** (for
  snapshots, see [Safety model](safety.md#snapshots)), **Ollama** (for the
  optional [AI advisor](ai-advisor.md)).

## 2. Get FSU

Choose **one** of the options below. Each box is complete: copy the whole box
and paste it into a terminal. It downloads FSU (if it is not there yet), sets
it up and finishes by printing the version, so you can see it worked.

### Option A — run without installing (recommended while testing)

Nothing is installed; FSU runs straight from the downloaded folder. An alias
in your `~/.bashrc` makes the `fsu` command available in every terminal, from
any directory.

```bash
[ -d ~/Fedora-Sentinel-Uninstaller ] || git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git ~/Fedora-Sentinel-Uninstaller
grep -qs "alias fsu=" ~/.bashrc || echo "alias fsu='PYTHONPATH=\$HOME/Fedora-Sentinel-Uninstaller/src python3 -m fsu'" >> ~/.bashrc
source ~/.bashrc
fsu --version
```

- To update later, paste: `git -C ~/Fedora-Sentinel-Uninstaller pull`
- To undo: delete the `alias fsu=…` line from `~/.bashrc` and the folder.
- Using zsh instead of bash? Replace `~/.bashrc` with `~/.zshrc` in the box.

> An alias typed directly in a terminal (without saving it in `~/.bashrc`)
> only lasts until that terminal is closed. If `fsu` suddenly "is not found",
> that is why — paste the Option A box again.

### Option A′ — just try it once, without any setup

Runs a single scan from the downloaded folder; nothing is configured.

```bash
[ -d ~/Fedora-Sentinel-Uninstaller ] || git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git ~/Fedora-Sentinel-Uninstaller
cd ~/Fedora-Sentinel-Uninstaller && PYTHONPATH=src python3 -m fsu scan
```

### Option B — install the `fsu` command, following the repository

Installs `fsu` in `~/.local/bin` (already in your `PATH` on Fedora), but keeps
using the downloaded folder: every update takes effect without reinstalling.

```bash
[ -d ~/Fedora-Sentinel-Uninstaller ] || git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git ~/Fedora-Sentinel-Uninstaller
pip install --user -e ~/Fedora-Sentinel-Uninstaller
fsu --version
```

- To update later, paste: `git -C ~/Fedora-Sentinel-Uninstaller pull`
- To undo, paste: `pip uninstall -y fedora-sentinel-uninstaller`

### Option C — regular install (a fixed copy)

```bash
[ -d ~/Fedora-Sentinel-Uninstaller ] || git clone -b dev https://github.com/ericksousa32/Fedora-Sentinel-Uninstaller.git ~/Fedora-Sentinel-Uninstaller
pip install --user ~/Fedora-Sentinel-Uninstaller
fsu --version
```

- To update later, paste:
  `git -C ~/Fedora-Sentinel-Uninstaller pull && pip install --user ~/Fedora-Sentinel-Uninstaller`

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
are gone, put them back.

See what is in quarantine and each item's ID:

```bash
fsu list-quarantine
```

Put back one item — replace the ID with one shown by `fsu list-quarantine`:

```bash
fsu restore 20261107T175332-473f1c
```

Or put **everything** back:

```bash
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
