"""Home root, ~/.local/state, launchers/services and XDG variables."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeRunner, default_responses
from fsu.collector import Collector
from fsu.inventory import CommandStatus, check_command
from fsu.models import Action, Candidate, Category
from fsu.paths import fsu_data_dir, scan_roots
from fsu.protected import category_of, is_protected
from fsu.quarantine import QuarantineError, QuarantineStore
from fsu.safeguard import revalidate, symlink_in_chain
from helpers import candidate, make


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _exe(path: Path) -> Path:
    _write(path, "#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _scan(home: Path, bindir: Path | None = None) -> dict[str, Candidate]:
    collector = Collector(
        home, FakeRunner(default_responses()), [], executable_dirs=[bindir] if bindir else []
    )
    report = collector.collect()
    return {str(c.path.relative_to(home)): c for c in report.candidates}


# -- home root (item 1) -----------------------------------------------------------------


@pytest.fixture
def dotted_home(home: Path, tmp_path: Path) -> Path:
    make(home / ".nuget/packages/x/1.0/x.nupkg", 300)  # owner dotnet* removed
    make(home / ".dotnet/tools/x")
    make(home / ".hexchat/servlist.conf")  # exact name of a removed package
    make(home / ".kwebkitpart-old/x")  # only a *variant* matches: never in $HOME
    make(home / ".aspnetcore/x")  # only *similar* to a removed package
    make(home / ".mystery/x")  # no evidence
    make(home / ".mozilla/firefox/profile/places.sqlite")  # protected user data
    make(home / ".wine/drive_c/x")  # protected user data
    make(home / ".ssh/id_ed25519")  # identity
    make(home / ".android/adbkey")  # identity
    make(home / ".bashrc")  # a file: never evaluated
    make(home / "Documents/report.odt")  # not hidden: never evaluated
    (home / ".linked").symlink_to(tmp_path)  # symlinked folder: never evaluated
    return home


def test_home_root_only_lists_exact_high_confidence(dotted_home: Path) -> None:
    found = _scan(dotted_home)
    home_items = {k: v for k, v in found.items() if v.category is Category.HOME}
    assert set(home_items) == {".nuget", ".dotnet", ".hexchat"}
    assert home_items[".nuget"].score == 0.80
    assert home_items[".nuget"].matched_name == "dotnet-runtime-8.0"
    assert home_items[".hexchat"].score == 0.85
    assert all(c.action is Action.QUARANTINE for c in home_items.values())


def test_home_root_counts_hidden_items(dotted_home: Path) -> None:
    report = Collector(
        dotted_home, FakeRunner(default_responses()), [], executable_dirs=[]
    ).collect()
    # .kwebkitpart-old, .aspnetcore, .mystery: below 0.70, so only counted
    assert report.low_confidence_hidden == 3
    protected = {p.name for p in report.protected_skipped}
    assert {".mozilla", ".wine", ".ssh", ".android"} <= protected
    assert ".bashrc" not in protected  # files are not even considered


def test_home_root_installed_program_wins(dotted_home: Path, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _exe(bindir / "hexchat")
    assert ".hexchat" not in _scan(dotted_home, bindir)


@pytest.mark.parametrize(
    ("name", "protected"),
    [
        (".ssh", True),
        (".gnupg", True),
        (".mozilla", True),
        (".minecraft", True),
        (".wine64", True),
        (".lmstudio", True),
        (".ollama", True),
        (".config", True),
        (".nuget", False),
        (".vscode", False),
        ("Documents", True),  # not hidden
    ],
)
def test_home_protection(home: Path, name: str, protected: bool) -> None:
    assert is_protected(home / name, home) is protected


# -- ~/.local/state (item 2) ------------------------------------------------------------


def test_state_lists_only_high_confidence(home: Path) -> None:
    make(home / ".local/state/hexchat/history")  # removed package: 0.85
    make(home / ".local/state/mystery/x")  # no evidence: hidden
    make(home / ".local/state/wireplumber/x")  # protected
    found = _scan(home)
    state = {k: v for k, v in found.items() if v.category is Category.STATE}
    assert set(state) == {".local/state/hexchat"}
    assert state[".local/state/hexchat"].score == 0.85


# -- launchers and services (item 3) ----------------------------------------------------


@pytest.mark.parametrize(
    ("line", "status"),
    [
        ("/usr/bin/true", CommandStatus.FOUND),
        ("/opt/gone/app --flag", CommandStatus.MISSING),
        ('"/home/u/My Apps/gone.AppImage" %F', CommandStatus.MISSING),
        ("/run/media/u/usb/tool", CommandStatus.UNKNOWN),
        ("env A=1 /opt/gone/app", CommandStatus.MISSING),
        ("sh -c 'something'", CommandStatus.UNKNOWN),
        ("python3 /home/u/script.py", CommandStatus.UNKNOWN),
        ("$HOME/bin/app", CommandStatus.UNKNOWN),
        ("fsu-surely-not-a-program-xyz", CommandStatus.MISSING_BARE),
        ("flatpak run com.discordapp.Discord", CommandStatus.FOUND),
        ("flatpak run --branch=stable org.gone.App", CommandStatus.MISSING),
        ("relative/path/app", CommandStatus.UNKNOWN),
        ("", CommandStatus.UNKNOWN),
    ],
)
def test_check_command(line: str, status: CommandStatus) -> None:
    flatpaks = frozenset({"com.discordapp.Discord"})
    assert check_command(line, [], flatpaks).status is status


def test_check_command_systemd_prefixes() -> None:
    assert check_command("-/opt/gone/app", [], None, systemd=True).status is CommandStatus.MISSING
    assert check_command("@/usr/bin/true x", [], None, systemd=True).status is CommandStatus.FOUND


def test_flatpak_unknown_without_inventory() -> None:
    assert check_command("flatpak run org.x.Y", [], None).status is CommandStatus.UNKNOWN


@pytest.fixture
def launcher_home(home: Path) -> Path:
    autostart = home / ".config/autostart"
    _write(autostart / "gone.desktop", "[Desktop Entry]\nExec=/opt/gone/bin/gone --tray\n")
    _write(
        autostart / "discord.desktop", "[Desktop Entry]\nExec=flatpak run com.discordapp.Discord\n"
    )
    _write(  # intentional override of a system autostart entry
        autostart / "baloo.desktop", "[Desktop Entry]\nHidden=true\nExec=/opt/gone/x\n"
    )
    _write(autostart / "script.desktop", "[Desktop Entry]\nExec=sh -c 'sleep 5; foo'\n")
    _write(autostart / "notes.txt", "not a launcher")
    units = home / ".config/systemd/user"
    _write(units / "sync.service", "[Service]\nExecStart=/opt/gone/bin/sync\n")
    _write(units / "sync.timer", "[Timer]\nOnCalendar=daily\n")
    _write(units / "backup.service", "[Service]\nExecStart=/usr/bin/true\n")
    _write(units / "backup.timer", "[Timer]\nUnit=backup.service\n")
    _write(units / "orphan.timer", "[Timer]\nUnit=nothing.service\n")
    _write(
        home / ".local/share/dbus-1/services/org.gone.Service.service",
        "[D-BUS Service]\nName=org.gone.Service\nExec=/opt/gone/bin/dbus-service\n",
    )
    _write(
        home / ".local/share/applications/gone-flatpak.desktop",
        "[Desktop Entry]\nExec=flatpak run org.gone.App\n",
    )
    return home


def test_launchers(launcher_home: Path) -> None:
    found = _scan(launcher_home)
    launchers = {Path(k).name: v for k, v in found.items() if v.category is Category.LAUNCHER}
    assert set(launchers) == {
        "gone.desktop",
        "sync.service",
        "sync.timer",
        "org.gone.Service.service",
        "gone-flatpak.desktop",
    }
    assert launchers["gone.desktop"].score == 0.85
    assert launchers["gone.desktop"].matched_name == "/opt/gone/bin/gone"
    assert launchers["sync.timer"].score == launchers["sync.service"].score
    assert "org.gone.App is not installed" in launchers["gone-flatpak.desktop"].evidence[0].detail


def test_launcher_counters(launcher_home: Path) -> None:
    report = Collector(
        launcher_home, FakeRunner(default_responses()), [], executable_dirs=[]
    ).collect()
    protected = {p.name for p in report.protected_skipped}
    assert "baloo.desktop" in protected  # Hidden=true is intentional
    # script.desktop (shell) and orphan.timer (unknown unit) are hidden
    assert report.low_confidence_hidden == 2
    # discord.desktop, backup.service, backup.timer
    assert report.installed_skipped >= 3


def test_launcher_quarantine_and_restore(launcher_home: Path) -> None:
    item = launcher_home / ".config/autostart/gone.desktop"
    cand = candidate(item, launcher_home, 0.85)
    assert cand.category is Category.LAUNCHER
    assert revalidate(cand, launcher_home) == []
    store = QuarantineStore(launcher_home)
    manifest = store.quarantine(cand)
    assert not item.exists()
    assert store.restore(manifest.id) == item


# -- XDG variables (item 4) -------------------------------------------------------------


def test_xdg_variables_are_honoured(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data = tmp_path / "xdg/config", tmp_path / "xdg/data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/ignored")  # invalid: default used
    roots = scan_roots(home)
    assert roots[Category.CONFIG] == (config,)
    assert roots[Category.CACHE] == (home / ".cache",)
    assert config / "autostart" in roots[Category.LAUNCHER]
    assert fsu_data_dir(home) == data / "fsu"

    make(config / "hexchat/x")
    make(home / ".config/hexchat/x")  # not the config home anymore
    report = Collector(home, FakeRunner(default_responses()), [], executable_dirs=[]).collect()
    paths = {c.path for c in report.candidates}
    assert config / "hexchat" in paths
    assert home / ".config/hexchat" not in paths
    assert category_of(config / "hexchat", home) is Category.CONFIG
    assert category_of(home / ".config/hexchat", home) is None


def test_quarantine_follows_xdg_data_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = home / "custom-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    make(data / "hexchat/x")
    store = QuarantineStore(home)
    manifest = store.quarantine(candidate(data / "hexchat", home, 0.85))
    assert (data / "fsu/quarantine" / manifest.id).is_dir()
    monkeypatch.delenv("XDG_DATA_HOME")
    # With another data home the entry is simply not visible; nothing is touched.
    assert QuarantineStore(home).entries() == []
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    assert store.restore(manifest.id) == data / "hexchat"


def test_symlink_in_chain(home: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere/autostart").mkdir(parents=True)
    (home / ".config").symlink_to(tmp_path / "elsewhere")
    assert symlink_in_chain(home / ".config/autostart", home)
    assert not symlink_in_chain(home / ".local/share", home)
    assert not symlink_in_chain(home, home)


def test_launcher_in_symlinked_config_is_skipped(home: Path, tmp_path: Path) -> None:
    _write(tmp_path / "dotfiles/autostart/gone.desktop", "[Desktop Entry]\nExec=/opt/gone/x\n")
    (home / ".config").symlink_to(tmp_path / "dotfiles")
    found = _scan(home)
    assert not any(v.category is Category.LAUNCHER for v in found.values())


def test_tampered_manifest_cannot_leave_the_scan_roots(home: Path) -> None:
    import json

    make(home / ".nuget/x")
    store = QuarantineStore(home)
    manifest = store.quarantine(candidate(home / ".nuget", home, 0.80))
    path = store.root / manifest.id / "manifest.json"
    data = json.loads(path.read_text())
    data["original_path"] = str(home / "sub" / ".nuget")  # not a direct child of $HOME
    path.write_text(json.dumps(data))
    with pytest.raises(QuarantineError):
        store.restore(manifest.id)
