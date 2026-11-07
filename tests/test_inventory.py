from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeRunner, default_responses, fixture_text
from fsu.inventory import (
    DesktopIndex,
    DnfHistory,
    FlatpakInventory,
    RpmInventory,
    is_noise_package,
    nevra_name,
)
from fsu.system import CommandError


@pytest.mark.parametrize(
    ("nevra", "name"),
    [
        ("hexchat-2.16.2-9.fc45.x86_64", "hexchat"),
        ("gimp-2:3.0.4-1.fc45.x86_64", "gimp"),
        ("aspnetcore-runtime-8.0-8.0.21-1.fc45.x86_64", "aspnetcore-runtime-8.0"),
        ("cowsay-3.8.4-1.fc45.noarch", "cowsay"),
    ],
)
def test_nevra_name(nevra: str, name: str) -> None:
    assert nevra_name(nevra) == name


@pytest.mark.parametrize(
    ("name", "noise"),
    [
        ("kf5-kparts", True),
        ("qt5-qtwebkit", True),
        ("python3-requests", True),
        ("gimp-libs", True),
        ("libreoffice-core", False),
        ("librewolf", False),
        ("hexchat", False),
        ("kwebkitpart", False),
    ],
)
def test_noise_packages(name: str, noise: bool) -> None:
    assert is_noise_package(name) is noise


def test_rpm_installed_names(fake_runner: FakeRunner) -> None:
    names = RpmInventory(fake_runner).installed_names()
    assert {"bash", "gimp", "linuxtoys"} <= names


def test_rpm_empty_inventory_is_an_error() -> None:
    with pytest.raises(CommandError, match="vazio"):
        RpmInventory(FakeRunner({("rpm",): "\n"})).installed_names()


def test_history_uses_single_range_call(fake_runner: FakeRunner) -> None:
    DnfHistory(fake_runner).removed_packages()
    info_calls = [c for c in fake_runner.calls if c[:3] == ("dnf5", "history", "info")]
    # IDs reais da fixture: 393..405
    assert info_calls == [("dnf5", "history", "info", "393..405", "--json")]


def test_history_removed_packages(fake_runner: FakeRunner) -> None:
    removed = {r.name: r for r in DnfHistory(fake_runner).removed_packages()}
    assert set(removed) == {
        "kwebkitpart",
        "kf5-kparts",
        "qt5-qtwebkit",
        "aspnetcore-runtime-8.0",
        "dotnet-runtime-8.0",
        "hexchat",
        "gimp",
    }
    # "Replaced" (upgrade do firefox) não conta como remoção.
    assert "firefox" not in removed
    # Vale a remoção mais recente: 396, não 393.
    assert removed["hexchat"].transaction_id == 396
    assert removed["kwebkitpart"].reason == "User"


def test_history_falls_back_to_individual_calls() -> None:
    responses = default_responses()
    one = (
        '[{"id": 398, "end_time": 1, "packages": [{"nevra": "a-1-1.x86_64", "action": "Remove"}]}]'
    )
    responses[("dnf5", "history", "info", "393..405")] = CommandError(["dnf5"], "spec inválida")
    responses[("dnf5", "history", "info")] = one
    history = DnfHistory(FakeRunner(responses), fallback_limit=3)
    removed = history.removed_packages()
    assert [r.name for r in removed] == ["a"]
    assert history.warnings and "uma a uma" in history.warnings[0]


def test_history_invalid_json_is_an_error() -> None:
    runner = FakeRunner({("dnf5", "history", "list"): "isto não é json"})
    with pytest.raises(CommandError, match="JSON inválido"):
        DnfHistory(runner).removed_packages()


def test_history_empty() -> None:
    runner = FakeRunner({("dnf5", "history", "list"): "[]"})
    assert DnfHistory(runner).removed_packages() == []


def test_flatpak_filters_translated_header_and_duplicates(fake_runner: FakeRunner) -> None:
    ids = FlatpakInventory(fake_runner).installed_app_ids()
    assert "ID de aplicativo" not in ids
    assert "com.hypixel.HytaleLauncher" in ids
    expected = {
        line.strip() for line in fixture_text("flatpak_list.txt").splitlines()[1:] if line.strip()
    }
    assert ids == expected


def _desktop(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")


def test_desktop_index(tmp_path: Path) -> None:
    apps = tmp_path / "applications"
    _desktop(
        apps,
        "code.desktop",
        "[Desktop Entry]\nName=VS Code\nExec=/usr/share/code/code --unity %F\n"
        "StartupWMClass=Code\n[Desktop Action new]\nExec=ignorado --new\n",
    )
    _desktop(apps, "org.inkscape.Inkscape.desktop", "[Desktop Entry]\nExec=flatpak run x\n")
    _desktop(apps, "foo.desktop", "[Desktop Entry]\nExec=env A=1 /usr/bin/foo-bin %U\n")
    names = DesktopIndex([apps, tmp_path / "inexistente"]).known_names()
    assert {"code", "Code", "org.inkscape.Inkscape", "Inkscape", "foo", "foo-bin"} <= names
    assert "run" not in names
    assert "ignorado" not in names
    assert "env" not in names
