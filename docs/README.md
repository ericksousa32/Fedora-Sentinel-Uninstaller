# Fedora Sentinel Uninstaller — Documentation

When you uninstall an application on Linux, the package manager removes the
program, but the files it created in your home folder stay behind: settings in
`~/.config`, data in `~/.local/share`, caches in `~/.cache`, and more. Over
time these leftovers pile up — sometimes hundreds of megabytes of them.

**FSU finds those leftovers and helps you remove them safely.** It never
deletes anything on its own: it explains what it found and why, moves what you
approve to a *quarantine* you can undo at any time, and only deletes for good
when you explicitly ask.

## FSU in one minute

| Step | Command | Changes anything? |
|---|---|---|
| What is left behind? | `fsu scan` | No |
| What would be cleaned? | `fsu quarantine` | No |
| Clean up | `fsu quarantine --apply` | Moves items to quarantine, after you confirm |
| Changed your mind? | `fsu restore --all` | Puts everything back |
| Days later, when sure | `fsu purge --older-than 7` | Deletes for good, after you confirm |

New here? Start with [Getting started](getting-started.md).

## Guides

Start here if you want to **use** FSU:

| Guide | What you will learn |
|---|---|
| [Getting started](getting-started.md) | Install FSU and do your first clean-up, step by step |
| [Reading the results](reading-the-results.md) | What scores, evidence lines and AI opinions mean, and how to decide |
| [Command reference](commands.md) | Every command and option, with examples and exit codes |
| [Troubleshooting & FAQ](troubleshooting.md) | "Why is X listed?", "Why is Y not?", common warnings |

Read these to **understand** FSU:

| Document | What it covers |
|---|---|
| [How it works](how-it-works.md) | Where FSU looks, what it compares against, how scores are given |
| [Safety model](safety.md) | Every protection between "found" and "deleted" |
| [AI advisor](ai-advisor.md) | The optional local AI: setup, privacy, what it can and cannot do |
| [Known limitations](limitations.md) | What FSU cannot know, and design decisions learned from real tests |
| [Development](development.md) | Architecture, tests, CI and the rules for changing FSU |

## The three promises

1. **Nothing happens without you.** Scans and plans change nothing. Moving
   asks for confirmation; deleting asks again.
2. **Everything is reversible until you purge.** Quarantine is a simple move
   inside your own home folder; `fsu restore` puts things back exactly as
   they were.
3. **Evidence, not guesses.** Only items with concrete evidence (a removed
   package, a missing program, an uninstalled Flatpak…) are proposed for
   clean-up. Everything else is shown with its facts, for you to judge.
