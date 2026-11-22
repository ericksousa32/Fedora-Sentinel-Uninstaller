# Safety model

FSU works in your home folder, where your settings and data live. Its design
starts from one rule: **a false "protected" only leaves a few files behind; a
false "residue" can lose your data.** When in doubt, FSU protects.

```
scan ──► plan ──► confirm ──► re-check ──► move (quarantine) ──► … ──► purge
 read     read     you say     just before    instant and          only on
 only     only     yes         each move      reversible           request
```

## What FSU never does

- **Never runs as root** — not directly, not through `sudo`, not through
  `pkexec`. Files that do not belong to you are left alone.
- **Never deletes during a clean-up.** Items are *moved* to a quarantine
  inside your home folder.
- **Never follows symbolic links.** A link is moved as a link; its target is
  never touched.
- **Never crosses file systems or Btrfs subvolumes.** Only instant
  same-filesystem moves are used; anything else is blocked.
- **Never overwrites** anything when restoring.
- **Never purges automatically.**
- **Never moves low-confidence items** unless you select them, and then only
  with a snapshot.

## Protected items

These are never candidates, whatever their score.

| Area | Examples |
|---|---|
| Desktop session | `dconf`, `autostart` (as a whole), `systemd`, `gtk-*`, `fontconfig`, `pipewire`, KDE Plasma/Frameworks files (`kdeglobals`, `kwin*`, `plasma*`, `krunnerrc`, …), GNOME files (`gnome-*`) |
| Security and identity | `keyrings`, `gnupg`, `.ssh`, `.pki`, `.android` (adb keys), `.docker`, `.kube`, `.aws`, `.password-store` |
| Your own data (in `$HOME`) | browser and mail profiles (`.mozilla`, `.thunderbird`), games (`.steam`, `.minecraft`), Windows prefixes (`.wine*`), local AI models (`.lmstudio`, `.ollama`), `.java`, `.fonts` |
| Shared system data | `flatpak`, `containers`, `applications`, `icons`, `fonts`, `mime`, `Trash`, `man` |
| FSU itself | its quarantine, log and history |

User-data folders in `$HOME` stay protected **even if their application was
removed**: uninstalling a game launcher does not make your saved games
disposable.

## Checks before every move

`fsu quarantine --apply` checks each item when planning **and again right
before moving it**:

| Check | Blocks the item when |
|---|---|
| Re-validation | it disappeared, changed type, became protected, or a folder on its path became a symbolic link |
| Ownership | any file inside belongs to another user or cannot be read |
| Mount points | a mount point or Btrfs subvolume is inside it |
| Same file system | it is on another file system than the quarantine |
| In use | one of your processes has files open in it or is working inside it, or the owning application is running |
| Score policy | its score requires a snapshot that cannot be taken (see below) |

The "in use" check reads `/proc`. Linux hides other users' processes from
normal users, so files held by system services cannot be seen — a documented
limitation.

## Quarantine

Each item is moved with a single atomic `rename` into
`~/.local/share/fsu/quarantine/<id>/payload`, next to a `manifest.json` that
records where it came from, its permissions, owner and timestamps. Because it
is a rename on the same file system:

- it is instant, even for gigabytes;
- permissions, timestamps, extended attributes and SELinux labels are kept
  exactly as they were;
- `fsu restore` puts it back identically.

The manifest is written *before* the move and updated *after* it, so an
interruption (power loss, Ctrl+C) is settled automatically the next time you
run a command that changes the quarantine.

**Restoring is defensive too.** The manifest is a file you can edit, so FSU
does not trust it blindly: it only restores into the original scan area,
refuses paths that leave it, and never overwrites an existing file.

**Purging** removes only what is inside the quarantine folder, never follows
symbolic links, and handles read-only folders.

**One operation at a time.** Quarantine, restore and purge take an exclusive
lock; a second FSU process trying to change the quarantine at the same time
stops with a clear message.

Every move, restore and purge is logged in `~/.local/share/fsu/log.jsonl`.

## Snapshots

Medium-confidence items (0.40 – 0.69) and list-only items you select are
clues, not proof. For those, FSU wants an extra safety net: a **Btrfs snapshot
of your home** taken with Snapper just before the move — one snapshot per
batch.

FSU never configures Snapper (that needs administrator rights). It uses a
Snapper configuration whose subvolume is the mount point of your home and that
your user is allowed to use. On Fedora, an administrator can set this up once:

Create the configuration for your home and allow your user to use it (asks
for your password once):

```bash
sudo snapper -c home create-config /home && sudo snapper -c home set-config ALLOW_USERS=$USER SYNC_ACL=yes
```

Optional — keep only the snapshots FSU asks for, with no automatic hourly
ones:

```bash
sudo snapper -c home set-config TIMELINE_CREATE=no
```

Without Snapper:

- medium items are blocked unless you add `--allow-without-snapshot`;
- selected list-only items are always blocked (there is no override).

## Privacy

- Everything runs locally. The optional AI talks only to a model on this
  machine and refuses "cloud" models (see [AI advisor](ai-advisor.md#privacy)).
- The inventory history stores only paths and names, in your own state folder.
- FSU does not read the contents of your data files. It looks at names,
  types, sizes and dates; the only files it reads are launcher and service
  definitions (`.desktop`, `.service`, `.timer`), to know which program they
  run, and a small marker file to recognise Plasma Browser Integration stubs.
