# Troubleshooting & FAQ

## Results

### "An app I use is listed as `list_only`."

That is expected and harmless. `list_only` means "FSU has no evidence either
way" — such items are never moved unless you select them. Common reasons:

- the folder name differs from the app name (`comfyui-desktop-2` vs.
  *Comfy Desktop*);
- the app is an AppImage or a tool bundled inside another app, so there is no
  package or program with that name to match.

### "An app I use is proposed for quarantine (0.70 or more)."

This should be rare, and it means the evidence is misleading — please check it:

- `removed package: X` — maybe you removed `X` and reinstalled the same app
  another way (e.g. as a Flatpak with a different name).
- `… does not exist` on a launcher — the launcher really points to a missing
  file; the app may live somewhere else now.

If it was quarantined anyway, `fsu restore <id>` puts it back. Consider
reporting the case (include the output of `fsu --version`): a wrong
high-confidence item is a bug.

### "I know X is a leftover, but FSU does not propose it."

FSU needs concrete evidence. Apps installed outside package managers (an
AppImage deleted together with its launcher, a downloaded binary) leave no
record of their removal. From now on, FSU's
[inventory history](how-it-works.md#fsus-inventory-history) records such
apps, so future removals *will* be detected. For past ones, select the item
yourself: `fsu quarantine --apply --select PATH` (requires a snapshot).

### "Why are hidden folders in my home not all listed?"

In `$HOME` itself FSU only shows hidden folders with concrete evidence (0.70+),
and always protects identity and user-data folders (`.ssh`, `.gnupg`,
`.mozilla`, `.steam`, `.wine`, …). The summary line tells you how many
low-confidence items were not shown.

## Warnings

### `N folder(s) in ~/.var/app skipped: they are recreated at login by Plasma Browser Integration`

KDE Plasma creates small helper folders for Flatpak browsers in `~/.var/app`
at every login, even for browsers you never installed. Removing them is
pointless — they come back. To stop it: *System Settings → Background Services
→ Plasma Browser Integration Flatpak Integration*.

### `no Snapper config for /home`

Medium-confidence and selected list-only items need a snapshot. See
[Snapshots](safety.md#snapshots) to set one up, or use
`--allow-without-snapshot` for medium items.

### `RPM inventory unavailable` / `DNF5 history unavailable` / `Flatpak not found`

FSU could not read a source of truth, so it skips the areas that depend on it
instead of guessing. On Fedora 41+ `rpm` and `dnf5` are always present; this
usually means you are not on Fedora or something is broken in the RPM
database.

### `<root> is a symlink; root skipped for safety`

One of the scanned folders (for example `~/.config`) is a symbolic link,
typically managed by a dotfiles tool. FSU does not work inside it to avoid
touching files outside your home.

### `AI review skipped: …`

The optional AI could not be used (Ollama not running, model not installed,
a cloud model was refused…). The scan continues normally without opinions.

## Commands

### `not running interactively; pass --yes to confirm`

You ran `--apply` or `purge` from a script or pipe. Add `--yes` if that is
what you want.

### `another FSU operation is in progress`

Only one quarantine/restore/purge can run at a time. Wait for the other one
to finish.

### `… already exists; refusing to overwrite it` (restore)

The application recreated its folder after the clean-up. Decide which copy to
keep: move the new one away and run `fsu restore <id>` again, or purge the
quarantined copy.

### Some items were `blocked`

The reason is printed next to each item:

| Reason | Meaning |
|---|---|
| `in-use` | A program has files open inside it. Close the program and try again. |
| `app-running` | The owning application is running. |
| `snapshot-required` | Needs a snapshot (or `--allow-without-snapshot` for medium items). |
| `list-only` | Low confidence; use `--select` if you really want it. |
| `foreign-owner` / `unreadable` | Some files belong to another user or cannot be read; FSU never uses root. |
| `mount-point` / `cross-filesystem` | Another file system or Btrfs subvolume is involved; FSU only does instant, same-filesystem moves. |
| `missing` / `changed` / `protected` | The item changed since the scan; scan again. |

## Performance

### "The scan takes about 20 seconds."

Almost all of it is `dnf5 history` reading its database (measure it with
`fsu scan --timings`). It does not depend on FSU and does not use the network.

### "FSU wrote files in `~/.local/state`."

Two things are written during a scan, both documented: FSU's inventory history
(`~/.local/state/fsu/`, skip with `--no-history`) and `dnf5`'s own log
(`~/.local/state/dnf5.log*`, rotated by dnf5, about 5 MB at most). Your data
is never changed by a scan.
