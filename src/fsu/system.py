"""Controlled execution of external commands.

Every FSU subprocess goes through :func:`run_command`: no shell, a timeout,
a fixed locale (the user's Fedora may be localized) and typed errors.
Modules receive a ``Runner`` by injection, so they can be tested without a
real Fedora system.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str]], str]

DEFAULT_TIMEOUT = 60.0


class CommandError(RuntimeError):
    """The command failed (non-zero exit status or timeout)."""

    def __init__(self, args: Sequence[str], message: str) -> None:
        self.command = tuple(args)
        super().__init__(f"{' '.join(self.command)}: {message}")


class CommandNotFoundError(CommandError):
    """The executable does not exist on this system."""


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    # Predictable output: English messages and headers, UTF-8.
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    return env


def run_command(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run ``args`` (without a shell) and return its stdout.

    Raises :class:`CommandNotFoundError` if the executable does not exist and
    :class:`CommandError` on failure or timeout.
    """
    if not args:
        raise ValueError("empty command")
    try:
        # Argument list, never a shell (S603 is allowed project-wide for this call).
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_environment(),
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(args, "executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(args, f"timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise CommandError(args, str(exc)) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        detail = stderr[-1] if stderr else "no error message"
        raise CommandError(args, f"exit status {completed.returncode}: {detail}")
    return completed.stdout
