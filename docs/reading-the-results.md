# Reading the results

Every candidate in `fsu scan` looks like this:

```
0.80  quarantine                 372.0 MiB  ~/.nuget
      └─ folder owned by removed package(s): dotnet-host, dotnet-runtime-8.0 (+5 more)
```

| Part | Meaning |
|---|---|
| `0.80` | **Score**: how confident FSU is that the item belongs to software that is gone (0 to 1). |
| `quarantine` | **Action** allowed for that score (see below). |
| `372.0 MiB` | Size on disk (symlinks count as 0 — their target is never touched). |
| `~/.nuget` | The item: a folder or file directly inside one of the scanned places. |
| `└─ …` | **Evidence**: why it got this score. Several reasons are separated by `;`. |

## Scores and actions

| Score | Action | What it means for you |
|---|---|---|
| **0.70 – 1.00** | `quarantine` | Concrete evidence. Included in `fsu quarantine --apply`. |
| **0.40 – 0.69** | `quarantine_with_snapshot` | A clue, not proof. Moved only with a Snapper snapshot, or if you explicitly accept the risk with `--allow-without-snapshot`. |
| **0.00 – 0.39** | `list_only` | No concrete evidence. Shown for information; never moved unless you pick it with `--select`, and then only with a snapshot. |

## Kinds of evidence

### Concrete evidence (0.70 and above)

| Evidence text | Score | What FSU verified |
|---|---|---|
| `app <id> is not installed` | 0.95 | `~/.var/app/<id>` exists but that Flatpak is not installed. |
| `removed package: <name>` | 0.85 | The DNF5 history shows `<name>` was removed and it is not installed now. |
| `<path> does not exist` | 0.85 | A launcher/autostart/service file runs a program that is gone. |
| `Flatpak <id> is not installed` | 0.85 | A launcher runs `flatpak run <id>` for an app that is not installed. |
| `removed package: <name> (via '<variant>')` | 0.80 | Same as above, through a naming convention: `kolourpaintrc` → `kolourpaint`. |
| `folder owned by removed package(s): …` | 0.80 | The folder has a known owner (e.g. `~/.nuget` → .NET) and all owners were removed. |
| `launcher points to a missing AppImage: <name>` | 0.75 | One of your launchers points to an AppImage file that no longer exists. |
| `<kind> <path> was present on <date> and is gone` | 0.75 | FSU's own [inventory history](how-it-works.md#fsus-inventory-history) saw the program or AppImage earlier; it is gone now. |
| `program '<name>' not found` | 0.75 | A launcher/service runs a program by name and it is nowhere in the search path. |

### Clues (below 0.70)

| Evidence text | Effect |
|---|---|
| `weak/moderate/strong name similarity (NN%) to removed package '<name>'` | 0.50 – 0.65, depending on the similarity. |
| `… name similarity (NN%) to installed app '<name>'` | Caps the score at 0.30: something installed has a similar name. |
| `no known match` | 0.20: FSU knows nothing about this name. |

Similarity labels: **strong** ≥ 85%, **moderate** ≥ 60%, **weak** below 60%.

### Facts (list-only items)

List-only items get one more line of **facts**. They never change the score —
they are there to help *you* decide:

```
last write 1 day(s) ago; no program, launcher or running process with exactly this name ('nuvio')
```

- **last write** — the most recent time any file inside was modified. Recent
  writes tell you when the app was last used, *not* whether it is still
  installed (apps are often removed right after their last use).
- **program / launcher / process** — whether something with *exactly* this
  name exists. Not finding one is weak evidence: many apps bundle their tools
  or use a different name.

### Other notes you may see

| Note | Meaning |
|---|---|
| `symlink: only the link is considered, never its target` | Moving it moves the link, not what it points to. |
| `owned by another user (uid N)` | It will be blocked at clean-up time; FSU never uses root. |
| `AI: in_use (0.90) - …` | Optional [AI advisor](ai-advisor.md) opinion (only with `--ai`). |
| `AI: uncertain - …` | The AI found the data insufficient — a perfectly valid answer. |
| `[demoted]` | The AI judged a medium item to be in use, so it was lowered to list-only. |

## The summary lines

```
20 candidates (6.4 GiB); 162 protected; 169 match something installed; 8 recreated by other software.
14 low-confidence item(s) in $HOME, ~/.local/state and launcher folders are not listed (only >= 0.70 is shown there).
```

| Counter | Meaning |
|---|---|
| **protected** | Items FSU never touches (session settings, keyrings, identity, user data folders…). See [Safety model](safety.md#protected-items). |
| **match something installed** | Items that belong to installed software — correctly ignored. |
| **recreated by other software** | Items another program recreates at every login (e.g. Plasma Browser Integration stubs); removing them is pointless. |
| **low-confidence … not listed** | In the "strict" areas (hidden folders in `$HOME`, `~/.local/state`, launchers) only items with concrete evidence are shown. |

## Deciding about list-only items

List-only items are the ones FSU cannot decide for you. A practical routine:

1. **Recognise the name?** If you know the app and still use it, ignore the
   item. If you know you removed it, it is probably a leftover.
2. **Look at the facts.** A large cache last written months ago, with no
   program of that name, deserves a closer look. Something written today
   probably belongs to something you use (or used very recently).
3. **Peek inside** (read-only): `ls -la ~/.cache/<name>`. Folder names like
   `logs/`, `Cache/` or a vendor name often reveal the owner.
4. **Optional second opinion:** `fsu scan --ai` (see [AI advisor](ai-advisor.md)).
5. **Still unsure? Leave it.** A few unused megabytes are cheaper than lost
   settings.

To clean a list-only item you have decided about, select it explicitly (this
always requires a [Snapper snapshot](safety.md#snapshots)):

```bash
fsu quarantine --apply --select ~/.cache/nuvio
```

## Machine-readable output

`fsu scan --json` prints everything above as JSON (candidates, evidence,
counters, warnings, timings and, with `--ai`, the opinions), for scripts or
your own reports.
