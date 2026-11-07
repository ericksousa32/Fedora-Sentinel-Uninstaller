from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeRunner, default_responses
from fsu.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, EXIT_USAGE, Context, main
from helpers import FakeOllama, make

SRC = Path(__file__).resolve().parents[1] / "src"


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def regular_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("PKEXEC_UID", raising=False)


@pytest.fixture
def cli_home(home: Path) -> Path:
    make(home / ".config/hexchat/servlist.conf", 100)  # removed package: 0.85
    make(home / ".config/aspnetcore/settings.json")  # similar to removed: medium
    make(home / ".cache/mystery/blob")  # no match: list-only
    return home


class Cli:
    def __init__(self, home: Path, stdin: io.StringIO | None = None) -> None:
        self.home = home
        self.now = 1_800_000_000.0
        self.stdin = stdin or io.StringIO()

    def __call__(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        ctx = Context(
            home=self.home,
            runner=FakeRunner(default_responses()),
            desktop_dirs=[],
            stdout=out,
            stderr=err,
            stdin=self.stdin,
            clock=lambda: self.now,
        )
        code = main(list(argv), ctx)
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def cli(cli_home: Path) -> Cli:
    return Cli(cli_home)


def _quarantined(home: Path) -> list[str]:
    root = home / ".local/share/fsu/quarantine"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# -- scan -------------------------------------------------------------------------------


def test_scan_text(cli: Cli, cli_home: Path) -> None:
    code, out, _err = cli("scan")
    assert code == EXIT_OK
    assert str(cli_home / ".config/hexchat") in out
    assert "Nothing was changed" in out
    assert _quarantined(cli_home) == []


def test_scan_json(cli: Cli) -> None:
    code, out, _err = cli("scan", "--json")
    data = json.loads(out)
    assert code == EXIT_OK
    assert {Path(c["path"]).name for c in data["candidates"]} >= {"hexchat", "mystery"}
    assert "ai" not in data


def test_scan_with_ai(cli: Cli) -> None:
    fake = FakeOllama()
    try:
        code, out, _err = cli("scan", "--ai", "--ollama-url", fake.url)
    finally:
        fake.close()
    assert code == EXIT_OK
    assert "AI: uncertain" in out
    assert "advice only" in out


def test_scan_ai_unavailable_still_works(cli: Cli) -> None:
    code, out, err = cli("scan", "--ai", "--ollama-url", "http://127.0.0.1:9")
    assert code == EXIT_OK
    assert "hexchat" in out
    assert "AI review skipped" in err


def test_scan_refuses_remote_ollama(cli: Cli) -> None:
    code, _out, err = cli("scan", "--ai", "--ollama-url", "http://10.0.0.1:11434")
    assert code == EXIT_ERROR
    assert "non-local" in err


def test_root_is_refused(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    code, _out, err = cli("scan")
    assert code == EXIT_ERROR
    assert "never as root" in err


# -- quarantine -------------------------------------------------------------------------


def test_quarantine_is_a_dry_run_by_default(cli: Cli, cli_home: Path) -> None:
    code, out, _err = cli("quarantine")
    assert code == EXIT_OK
    assert "would-quarantine" in out
    assert "Dry run" in out
    assert "list-only item(s) not considered" in out
    assert (cli_home / ".config/hexchat").exists()
    assert _quarantined(cli_home) == []


def test_quarantine_apply_yes(cli: Cli, cli_home: Path) -> None:
    code, out, _err = cli("quarantine", "--apply", "--yes")
    assert code == EXIT_BLOCKED  # the medium item needs a snapshot
    assert not (cli_home / ".config/hexchat").exists()
    assert (cli_home / ".config/aspnetcore").exists()
    assert (cli_home / ".cache/mystery").exists()
    assert len(_quarantined(cli_home)) == 1
    assert "fsu restore" in out


def test_quarantine_medium_with_explicit_confirmation(cli: Cli, cli_home: Path) -> None:
    code, _out, _err = cli("quarantine", "--apply", "--yes", "--allow-without-snapshot")
    assert code == EXIT_OK
    assert len(_quarantined(cli_home)) == 2


def test_quarantine_needs_yes_when_not_interactive(cli: Cli, cli_home: Path) -> None:
    code, _out, err = cli("quarantine", "--apply")
    assert code == EXIT_USAGE
    assert "--yes" in err
    assert _quarantined(cli_home) == []


@pytest.mark.parametrize(("answer", "moved"), [("n\n", 0), ("\n", 0), ("y\n", 1)])
def test_quarantine_interactive_confirmation(cli_home: Path, answer: str, moved: int) -> None:
    cli = Cli(cli_home, TtyInput(answer))
    _code, out, _err = cli("quarantine", "--apply")
    assert "[y/N]" in out
    assert len(_quarantined(cli_home)) == moved


def test_select_unknown_path(cli: Cli, cli_home: Path) -> None:
    code, _out, err = cli("quarantine", "--select", str(cli_home / ".config/nope"))
    assert code == EXIT_USAGE
    assert "--select" in err


def test_selected_list_only_item_needs_snapshot(cli: Cli, cli_home: Path) -> None:
    mystery = str(cli_home / ".cache/mystery")
    code, out, _err = cli("quarantine", "--apply", "--yes", "--select", mystery)
    assert code == EXIT_BLOCKED
    assert "snapshot-required" in out
    assert (cli_home / ".cache/mystery").exists()


# -- list / restore / purge -------------------------------------------------------------


def test_list_restore_roundtrip(cli: Cli, cli_home: Path) -> None:
    assert cli("list-quarantine")[1].strip() == "Quarantine is empty."
    cli("quarantine", "--apply", "--yes")
    code, out, _err = cli("list-quarantine")
    assert code == EXIT_OK
    assert str(cli_home / ".config/hexchat") in out
    data = json.loads(cli("list-quarantine", "--json")[1])
    entry_id = data[0]["id"]
    code, out, _err = cli("restore", entry_id)
    assert code == EXIT_OK
    assert (cli_home / ".config/hexchat/servlist.conf").exists()
    assert _quarantined(cli_home) == []


def test_restore_all(cli: Cli, cli_home: Path) -> None:
    cli("quarantine", "--apply", "--yes", "--allow-without-snapshot")
    code, out, _err = cli("restore", "--all")
    assert code == EXIT_OK
    assert out.count("restored") == 2


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["restore"], EXIT_USAGE),
        (["restore", "--all", "20260101T000000-abcdef"], EXIT_USAGE),
        (["restore", "20260101T000000-abcdef"], EXIT_ERROR),
        (["restore", "../../etc"], EXIT_ERROR),
    ],
)
def test_restore_errors(cli: Cli, argv: list[str], expected: int) -> None:
    assert cli(*argv)[0] == expected


def test_purge_with_yes(cli: Cli, cli_home: Path) -> None:
    cli("quarantine", "--apply", "--yes")
    entry_id = _quarantined(cli_home)[0]
    code, out, _err = cli("purge", entry_id, "--yes")
    assert code == EXIT_OK
    assert f"purged {entry_id}" in out
    assert _quarantined(cli_home) == []
    assert not (cli_home / ".config/hexchat").exists()


def test_purge_needs_confirmation(cli: Cli, cli_home: Path) -> None:
    cli("quarantine", "--apply", "--yes")
    code, _out, _err = cli("purge", "--all")
    assert code == EXIT_USAGE
    assert len(_quarantined(cli_home)) == 1


def test_purge_interactive_no(cli_home: Path) -> None:
    Cli(cli_home)("quarantine", "--apply", "--yes")
    code, out, _err = Cli(cli_home, TtyInput("n\n"))("purge", "--all")
    assert code == EXIT_OK
    assert "PERMANENTLY" in out
    assert len(_quarantined(cli_home)) == 1


def test_purge_older_than(cli: Cli, cli_home: Path) -> None:
    cli("quarantine", "--apply", "--yes")
    assert cli("purge", "--older-than", "30", "--yes")[1].strip() == "Nothing to purge."
    cli.now += 31 * 86400
    code, out, _err = cli("purge", "--older-than", "30", "--yes")
    assert code == EXIT_OK
    assert "purged" in out
    assert _quarantined(cli_home) == []


def test_purge_unknown_id(cli: Cli) -> None:
    code, _out, err = cli("purge", "20260101T000000-abcdef", "--yes")
    assert code == EXIT_ERROR
    assert "not in quarantine" in err


# -- entry points -----------------------------------------------------------------------


def test_python_dash_m(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-m", "fsu", "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("fsu ")


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for command in ("scan", "quarantine", "list-quarantine", "restore", "purge"):
        assert command in out


def test_dry_run_creates_nothing(cli: Cli, cli_home: Path) -> None:
    cli("quarantine")
    assert not (cli_home / ".local/share/fsu").exists()


def test_concurrent_operation_is_refused(cli: Cli, cli_home: Path) -> None:
    from fsu.quarantine import QuarantineStore

    with QuarantineStore(cli_home).lock():
        code, _out, err = cli("quarantine", "--apply", "--yes")
    assert code == EXIT_ERROR
    assert "in progress" in err
    assert (cli_home / ".config/hexchat").exists()
