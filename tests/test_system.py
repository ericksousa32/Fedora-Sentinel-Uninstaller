from __future__ import annotations

import pytest

from fsu.system import CommandError, CommandNotFoundError, run_command


def test_returns_stdout() -> None:
    assert run_command(["echo", "olá"]) == "olá\n"


def test_forces_predictable_locale() -> None:
    output = run_command(["env"])
    assert "LC_ALL=C.UTF-8" in output.splitlines()


def test_missing_executable() -> None:
    with pytest.raises(CommandNotFoundError):
        run_command(["fsu-command-that-does-not-exist"])


def test_nonzero_exit_is_an_error_with_stderr() -> None:
    with pytest.raises(CommandError, match="exit status 3: failed"):
        run_command(["sh", "-c", "echo failed >&2; exit 3"])


def test_timeout() -> None:
    with pytest.raises(CommandError, match="timed out"):
        run_command(["sleep", "5"], timeout=0.2)


def test_no_shell_interpretation() -> None:
    # Metacharacters reach the program literally: there is no shell.
    assert run_command(["echo", "$HOME;", "rm", "-rf", "x"]) == "$HOME; rm -rf x\n"


def test_empty_command() -> None:
    with pytest.raises(ValueError, match="empty"):
        run_command([])
