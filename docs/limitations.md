# Known limitations and design decisions

FSU was developed and tested step by step on a real Fedora 45 system (KDE
Plasma). Several decisions below come directly from what those tests showed.
They are written down so that nobody "fixes" them back by accident.

## What FSU cannot know

**Removals that nobody recorded.** RPM and Flatpak keep a history; an
AppImage deleted together with its launcher, or a program downloaded and
later removed, leaves no trace. FSU's
[inventory history](how-it-works.md#fsus-inventory-history) closes this gap
from your first scan onwards, but cannot recover older removals. Such items
stay `list_only`.

**Who owns a folder with an unrelated name.** Names are only clues. On the
test system, one application stored its data under an old name
(`hytale-rich-presence`) and later under a new one (`statusify`); even the
user's memory of which one was in use turned out to be inaccurate. Only
concrete evidence reaches 0.70.

**Other users' processes.** A normal user cannot see which files a system
service has open, so the "in use" check covers only your own processes.

**Fedora Atomic** (Silverblue, Kinoite): packages removed with `rpm-ostree`
are not in the DNF5 history, so fewer RPM leftovers reach high confidence.
Flatpak, AppImage and launcher evidence work normally.

**Desktop coverage.** The list of protected session files was built on KDE
Plasma and also covers common GNOME items. On other desktops some session
files may show up as `list_only` — harmless noise, since list-only items are
never moved unless selected.

## Design decisions

**Access times are not used.** Programs that sweep files — indexers, search
tools, launchers reading other apps' settings, backups — update access times.
On the test system, 142 files belonging to unrelated applications were
"accessed" within the same minute. FSU therefore only uses modification times
("last write").

**Weak negative evidence is shown, never scored, never sent to the AI.**
"No program with this name was found" proves little: apps often bundle their
tools or use another name. People can weigh that; a model could not — when it
received this line, it called an in-use 3.6 GB game runtime a residue, despite
being told such absences are weak evidence.

**The AI is an advisor, never an executor.** It can only lower an item, never
raise one, and it receives neutral input only (no scores, sizes, dates or
negative findings). See [AI advisor](ai-advisor.md).

**Prompt changes follow general rules only.** Tuning the prompt to get one
item "right" on one system made it worse elsewhere (the model started quoting
the prompt instead of reading the data). Tests now check that the prompt names
no specific software and treats "installed" and "removed" symmetrically.

**Strict areas show only concrete evidence.** In `$HOME` itself, in
`~/.local/state` and in launcher folders, items below 0.70 are counted but not
listed: mistakes there cost more than a few leftover files.

**Folders with user data stay protected after their app is gone.** A removed
game launcher does not make saved games disposable; a removed browser does not
make its profile (passwords, bookmarks) disposable.

**No copy-based moves.** Moving across file systems would mean copying and
deleting; FSU only does atomic same-filesystem moves and blocks the rest.

## Side effects of a scan

A scan never changes your data, but these files are written:

| File | By | Why |
|---|---|---|
| `~/.local/state/fsu/inventory.json` | FSU | the inventory history (`--no-history` skips it) |
| `~/.local/state/dnf5.log*` | `dnf5` | dnf5 logs its own runs, including `dnf5 history`; rotated by dnf5, about 5 MB at most |
| folder access times | the kernel | listing a folder updates its access time (FSU does not use access times) |

## systemd user units

Quarantining a `.service` or `.timer` leaves its enabling symlink in
`~/.config/systemd/user/*.wants/` pointing nowhere until you restore it or run
`systemctl --user disable <unit>`. Since the unit's program was already gone,
the unit was failing anyway.

## Performance

A full scan takes about 20 seconds on a system with ~400 DNF5 transactions,
almost entirely inside `dnf5 history info` (the same with `--cacheonly`, so it
is not network-related). FSU's own work takes about one second. Reading DNF5's
database directly would be faster but depends on an undocumented format, so
the public command is used.
