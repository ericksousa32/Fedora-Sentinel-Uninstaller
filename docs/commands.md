# Command reference

```
fsu {scan, quarantine, list-quarantine, restore, purge} [options]
fsu --version
```

All commands refuse to run as root, through `sudo` or through `pkexec`.

## Version and help

Show the installed version. Handy to check that the alias or the installation
works, and worth including when you report a problem:

```bash
fsu --version
```

List the commands with a one-line description of each:

```bash
fsu --help
```

Show every option of one command — replace `restore` with the command you
want to know about:

```bash
fsu restore --help
```

## `fsu scan`

Lists residue candidates. Changes nothing in your files (it only updates
FSU's own [inventory history](how-it-works.md#fsus-inventory-history)).

| Option | Effect |
|---|---|
| `--json` | Machine-readable output. |
| `--timings` | Prints the time spent in each phase (to standard error). |
| `--no-history` | Do not update the inventory history (existing history is still used). |
| `--ai` | Ask the local [AI advisor](ai-advisor.md) about ambiguous items. Off by default. |
| `--model NAME` | Ollama model to use with `--ai` (default: an installed local model). |
| `--ollama-url URL` | Ollama address (default `http://127.0.0.1:11434`; only this machine is accepted). |

Examples — each box is one action:

```bash
fsu scan
```

```bash
fsu scan --ai --model gpt-oss:20b
```

```bash
fsu scan --json > ~/fsu-report.json
```

## `fsu quarantine`

Without `--apply`: shows the plan — what would move, what is blocked and why.
**Changes nothing.**

With `--apply`: shows the plan, asks `[y/N]`, then moves the approved items to
the quarantine, re-checking each one right before the move.

| Option | Effect |
|---|---|
| `--apply` | Actually move the items. |
| `--yes`, `-y` | Do not ask for confirmation. Required when there is no interactive terminal (scripts). |
| `--select PATH` | Include a list-only item you chose. Repeatable. Always requires a Snapper snapshot. |
| `--allow-without-snapshot` | Include medium-confidence items (0.40 – 0.69) even if no snapshot can be taken. |
| `--no-history`, `--ai`, `--model`, `--ollama-url` | Same as in `fsu scan`. |

Examples — each box is one action. Paste only one at a time: `--apply` waits
for your `y`/`N` answer, and anything pasted after it would be read as that
answer.

See the plan (changes nothing):

```bash
fsu quarantine
```

Clean the high-confidence items:

```bash
fsu quarantine --apply
```

Also include medium-confidence items, without a snapshot (you accept the risk):

```bash
fsu quarantine --apply --allow-without-snapshot
```

Also include a list-only item you chose (needs a Snapper snapshot) — replace
the path with yours:

```bash
fsu quarantine --apply --select ~/.cache/example-app
```

What is included in the plan:

| Score | Included when |
|---|---|
| ≥ 0.70 | Always. |
| 0.40 – 0.69 | A Snapper snapshot can be taken, or `--allow-without-snapshot` is given. |
| < 0.40 | Only with `--select`, and only if a snapshot can be taken. |

When a snapshot is needed, FSU takes **one** snapshot for the whole batch,
before moving anything.

## `fsu list-quarantine`

Lists the items in quarantine: ID, age in days, size, original location and
the snapshot taken for it (if any).

| Option | Effect |
|---|---|
| `--json` | Full manifests as JSON. |

```
20261107T175332-473f1c     0d       668 B  ~/.config/bleachbit
20261107T175333-41c2b6     0d   372.0 MiB  ~/.nuget
```

## `fsu restore`

Moves items back to where they came from. It **never overwrites**: if
something already exists at the original location (for example, the app
recreated its folder), that item is not restored and FSU tells you.

One item — replace the ID with one shown by `fsu list-quarantine`:

```bash
fsu restore 20261107T175332-473f1c
```

Several items at once — again, use your own IDs:

```bash
fsu restore 20261107T175332-473f1c 20261107T175333-41c2b6
```

Everything:

```bash
fsu restore --all
```

## `fsu purge`

**Permanently deletes** items from the quarantine — the only irreversible
command. Always asks for confirmation unless `--yes` is given.

| Option | Effect |
|---|---|
| `ID ...` | These items. |
| `--all` | Every item in quarantine. |
| `--older-than DAYS` | Items quarantined more than DAYS days ago. |
| `--yes`, `-y` | Do not ask for confirmation. |

Items quarantined more than 7 days ago:

```bash
fsu purge --older-than 7
```

One item — replace the ID with one shown by `fsu list-quarantine`:

```bash
fsu purge 20261107T175333-41c2b6
```

Nothing is ever purged automatically.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Error (for example: running as root, an invalid ID, another FSU operation in progress). |
| `2` | Usage error (for example: `--apply` without `--yes` in a script, `--select` of a path that is not a candidate). |
| `3` | Some items were blocked (the reasons are printed). |
| `130` | Interrupted with Ctrl+C. Items already moved stay in quarantine and can be restored. |

## Files FSU uses

| Path | Content |
|---|---|
| `~/.local/share/fsu/quarantine/<id>/` | Each quarantined item (`payload`) and its `manifest.json`. |
| `~/.local/share/fsu/log.jsonl` | One line per quarantine, restore and purge. |
| `~/.local/state/fsu/inventory.json` | The [inventory history](how-it-works.md#fsus-inventory-history). |

These follow `XDG_DATA_HOME` and `XDG_STATE_HOME` when you have set them.
